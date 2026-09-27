#!/bin/sh
# The entrypoint for this folder, per convention 1. It is POSIX sh, per
# convention 2; what it drives is a Python package, which is the part of this
# folder that is new to the repository.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
# The interpreter, not the name. `python3` on a bare PATH is Xcode's 3.9,
# which has no `datetime.UTC` -- and `sd_db/backup.py:45` imports it, so every
# verb here dies three imports deep with an ImportError rather than a
# sentence. It survived this long because a login shell puts Homebrew first;
# it broke the moment `local-sd-plan` called `work register` from the runner's
# exec'd environment, which carries `/usr/bin:/bin:/usr/sbin:/sbin` and
# nothing else. Same fix, same shape and same reasoning as
# `local-sd-plan/sd-plan.sh:15`, which hit it first. An explicitly set PYTHON
# is honoured but still checked: refusing in a sentence beats the traceback.
# The floor is `requires-python` in pyproject.toml, not the one import that
# first forced a floor: accepting less runs a package its own metadata refuses.
python_is_new_enough() {
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)' 2>/dev/null
}

# Called by the verbs, not at load: help and usage need no interpreter, and a
# machine without one still gets them.
resolve_python() {
    if [ -n "${PYTHON:-}" ]; then
        if ! command -v "$PYTHON" >/dev/null 2>&1; then
            echo "sd-db: PYTHON=$PYTHON is not an executable command" >&2
            exit 1
        fi
        python_is_new_enough "$PYTHON" || {
            echo "sd-db: $PYTHON is too old; sd_db needs Python 3.13 or newer" >&2
            exit 1
        }
        return 0
    fi
    for candidate in \
        "${SD_DB_PYTHON:-}" \
        "$HOME/repos/platypeeps/sd-ai-command-pack/.venv/bin/python" \
        /opt/homebrew/bin/python3 \
        python3
    do
        [ -n "$candidate" ] || continue
        if python_is_new_enough "$candidate"; then
            PYTHON="$candidate"
            return 0
        fi
    done
    echo "sd-db: no Python 3.13 or newer found; pyproject.toml requires it" >&2
    exit 1
}

# Which `sd_db` the verbs that open a database run: this checkout's, or the
# copy installed into $PYTHON (sd:1765). The primary checkout is shared by
# several sessions and nobody may move its HEAD, so it can sit behind
# `origin/main` for hours. The pack installs a copy pinned to a commit, and
# that copy does not follow the checkout. A checkout one migration behind the
# live database used to refuse every verb, while the interpreter it ran held
# a copy that could open it.
# `auto`, the default, runs the installed copy only when it is built for a
# newer schema than the checkout; a tie or an older copy runs the checkout, so
# a branch that adds a migration still runs its own. `checkout` and
# `installed` force one side. The checkout's version is read from its file:
# the probe runs `-I`, which keeps the checkout off the path.
choose_library() {
    LIBRARY=checkout
    choice="${SD_DB_LIBRARY:-auto}"
    case "$choice" in
        checkout) return 0 ;;
        installed|auto) ;;
        *)
            echo "sd-db: SD_DB_LIBRARY=$choice is not one of auto, checkout or installed" >&2
            exit 1
            ;;
    esac
    # A copy of this script beside no package reads nothing here and keeps
    # the old behaviour; `set -e` must not end the script on it, because the
    # backup's failure mail comes after.
    checkout_schema="$(sed -n 's/^SCHEMA_VERSION = \([0-9][0-9]*\)$/\1/p' "$DIR/sd_db/schema.py" 2>/dev/null)" || checkout_schema=""
    # `-I`: neither PYTHONPATH nor the working directory may stand in for
    # the installed copy. A copy that resolves into this folder is the
    # checkout, whatever put it on the path.
    probe="$("$PYTHON" -I -c 'import pathlib, sd_db.schema as s
print(s.SCHEMA_VERSION, pathlib.Path(s.__file__).resolve().parent.parent)' 2>/dev/null)" || probe=""
    installed_schema="${probe%% *}"
    installed_root="${probe#* }"
    case "$installed_schema" in
        ""|*[!0-9]*) installed_schema="" ;;
    esac
    if [ -z "$installed_schema" ] || [ "$installed_root" = "$(cd "$DIR" && pwd -P)" ]; then
        if [ "$choice" = installed ]; then
            echo "sd-db: SD_DB_LIBRARY=installed, and $PYTHON has no installed sd_db apart from this checkout" >&2
            exit 1
        fi
        return 0
    fi
    if [ "$choice" = installed ]; then
        LIBRARY=installed
        return 0
    fi
    if [ -n "$checkout_schema" ] && [ "$installed_schema" -gt "$checkout_schema" ]; then
        LIBRARY=installed
        echo "sd-db: this checkout's sd_db is built for schema $checkout_schema and $PYTHON has one built for $installed_schema; running the installed copy (SD_DB_LIBRARY=checkout runs the checkout)" >&2
    fi
}

# Replace the shell with the chosen library's interpreter. Inside `$(...)`
# it replaces only the subshell, which is what `backup` needs.
exec_library() {
    if [ "$LIBRARY" = installed ]; then
        exec "$PYTHON" -I "$@"
    fi
    PYTHONPATH="$DIR" exec "$PYTHON" "$@"
}

# The companion pack the acceptance tests drive. CI sets SD_ACCEPTANCE_PACK to
# its own checkout; a test that finds no pack at the default (the checkout's
# sibling `pack`, which is CI's layout and nobody's machine) fails, because a
# skipped test fails CI. So a bare local `sd-db.sh test` failed exactly one
# test and read as a regression. The pack path below is the one the Python
# candidates already assume. Only when it holds `bin/sd`, and never over an
# explicit value: a machine without the pack still fails the test, in the
# test's own sentence.
default_acceptance_pack() {
    pack="$HOME/repos/platypeeps/sd-ai-command-pack"
    if [ -z "${SD_ACCEPTANCE_PACK:-}" ] && [ -f "$pack/bin/sd" ]; then
        SD_ACCEPTANCE_PACK="$pack"
        export SD_ACCEPTANCE_PACK
    fi
}
# The cron mail path, overridable so a test can record the mail instead of
# sending one.
# `notify` is the bin-links name; a PATH without it falls back to the script
# it links to, so a failed backup still mails.
if [ -n "${SD_NOTIFY:-}" ]; then
    NOTIFY="$SD_NOTIFY"
elif command -v notify >/dev/null 2>&1; then
    NOTIFY=notify
else
    NOTIFY="$DIR/../local-notify/notify.sh"
fi

usage() {
    cat >&2 <<'USAGE'
sd-db.sh — the one database, and the fixture harness both faces test with.

Usage: sd-db.sh <command>

  init        Create ~/.local/share/sd/sd.db, bring it to the current schema
              version, and seed provider and bill from providers.yaml.
  migrate     Apply every migration the database has not seen. Run it with
              the dashboard and the runner stopped, after `backup` (which
              copies a database it cannot yet write). Nothing migrates on
              open.
  status      The path, the schema version, the version this library was
              built for, any unresolved state record, and how many key
              column values are absolute under this home (0 after
              migration 014).
  restore DIR Put a dated backup directory back in place. The restore is a
              record, not a permission: it lands unreconciled, and
              `sd restore resume` clears it.
  repo add P  Register the checkout at P. The `repo` table is what the
              `docs/work` migration enumerates from, so an empty table makes
              its criterion pass over nothing.
  repo seed   Register every checkout repo-sync's `repos.common.conf` and
              `repos.<profile>.conf` in `<config>/repo-sync/` name that this
              machine has actually cloned (<config> is $SYSTEM_TOOLS_CONFIG,
              default ~/.config/system; the profile is
              $REPO_SYNC_PROFILE, default personal). Fails naming the path
              when the profile conf is absent. `repo seed CONF` reads that
              one file instead.
  repo list [--managed]
              The repositories the table holds, which is the enumeration,
              each with its status source, its managed flag (yes|no) and its
              runner merge setting, which stays the last field. `--managed`
              prints only the rows marked managed.
  repo runner-merge PATH manual|auto
              Set whether the runner may merge this repository's work by
              itself. `auto` lets it open the exclusive merge lane; `manual`
              ends the item `ready_to_send` for a person, which is the
              default every row starts at. The path must already be
              registered; this writes no new row.
  repo managed PATH yes|no
              Mark whether the operator manages this repository. Every row
              starts at `no`. The path must already be registered; this
              writes no new row.
  repo remove PATH [--with-items] --who NAME --reason TEXT
              [--apply --if-fingerprint HEX]
              Preview what retiring the repository row at PATH would take,
              or apply it. Without --with-items a repository that still
              owns items is refused. The preview names every row, every
              refusal, the journal files that move after the commit, what
              is left, and its fingerprint; its last line is the apply
              command. Exit 0 is a clean preview, 3 a preview with
              refusals. The apply takes a backup first, removes the rows
              in one transaction that files the record as a cron-report
              item, then moves each run's journal pair into
              runner-recovery-evidence/removed-<fingerprint>/. Exit 4:
              the rows are gone and the record is filed, but the move did
              not finish; the printed lines finish it. A signal before the
              commit removes nothing and exits 1.
  item remove ID --who NAME --reason TEXT [--apply --if-fingerprint HEX]
              The same for one item row and what hangs off it: its notes,
              assignments, runs and leases. A row an importer owns is
              removed with a warning that the next import brings it back.
  work register PATH
              Register the docs/work item at PATH (relative to the
              repository enclosing the working directory) as the row
              that owns its status. This is what `import docs-work`
              used to do and cannot any more: every repository has
              retired the file source, so a folder made after the
              cutover has no row until this makes one. Title and date
              come from the frontmatter, the commit from git, and the
              status is always `planning`. The branch is the one this
              checkout is on when that is a local branch other than
              the default (a runner clone on plan/<slug>); on the
              default, or detached, it is left NULL for `sd runner
              prepare --branch` to fill -- never `origin/main`, which
              names no head. Registering twice reports the existing
              row and changes nothing.
  import S    Freeze one source, import it, and verify the rows against it.
              S is index, docs-work, register, vault or issues. Idempotent:
              a second run reports the same counts with zero new rows. It
              retires nothing, whatever S is -- `retire` is the other verb.
              For docs-work, a repository that has retired is a reported
              no-op, not a failure: its status lives in the row. Once every
              registered repository has, the verb says so and exits 0;
              `work register` is how a new item gets a row.
  verify S    Compare one source with the rows by identity and content,
              never by count, and name every difference. A retired
              docs-work repository is reported and left out, as `import`
              does; a fleet with none left to read is a no-op that exits 0.
  retire S    Hand S over to the database, once. It refuses under a pack
              whose sd_lib cannot answer `status_marker` and `delivered`,
              naming the version;
              without a `verified` row for what it just froze, naming the
              differences; and on an uncommitted file, naming it. Then it
              imports and verifies S once more, takes a backup, sets each
              repository's status_source to `row`, and makes one commit that
              removes every active item's `status:` line and adds
              docs/work/.status-source. The archive keeps its lines: they
              are records of what was. Only docs-work has a retire step.
  backup [--destination PATH] [--keep all|N | --keep-days N]
         [--require-mount PATH] [--no-row-prune]
              Snapshot the database, then restore the snapshot to prove it:
              VACUUM INTO a dated directory under
              /Volumes/local/Backup/sd-backups/ (refused while /Volumes/local
              is not mounted; --destination or SD_DB_BACKUP_ROOT names
              another root and skips that check),
              copy providers.yaml and commands.yaml beside it, reopen the
              copy, check its integrity, find this run's checkpoint row,
              compare every table's count against the source. Default `all`
              deletes nothing. Explicit N retains N verified owned backups;
              legacy, changed, linked, and unrelated directories stay put.
              --keep-days N instead deletes verified owned backups whose
              day lies wholly more than N days back, so a window of 7 keeps
              between 7 and 8 days of runs; it takes no count, and only
              the directories past the window are opened.
              --destination changes the backup root for this invocation.
              --require-mount PATH refuses, writing nothing and mailing the
              failure, unless PATH is a mounted volume and the destination
              lands on it: a detached drive's mount point is a plain
              directory on the boot disk.
              --no-row-prune skips the row prune below and its report item,
              for a backup taken more often than nightly; the hourly
              `sd-db-backup-hourly` job passes it.
              A database waiting for `migrate` is copied read-only and
              proved by counts; no checkpoint row, so retention never
              owns it and no prune follows. This is what the scheduled
              `sd-db-backup` job runs.
              After the snapshot passed -- never before -- the nightly row
              prune runs: `exec` output files older than ninety days are
              removed and their notes marked `output expired` (the note,
              with entry, arguments and exit code, stays); `heartbeat`
              rows are cut to one per key; a clean `report` row -- one that
              said it needed no attention, with no unresolved followup and
              past seven days -- is settled to `done`, so the week's runs
              stay visible and the older ones stop accumulating in
              `planning`; `cost` rows are never pruned.
              It writes one `report` item with the counts and prints them
              on this verb's one output line.
              On failure it sends one email through the cron mail path,
              writes nothing to the database, and exits 1; a failed backup
              means no prune. Exit 2 is a different answer and not a
              failure: the backup passed and the prune ran, and the SOURCE
              has foreign key violations. That mail names the tables. The
              snapshot it took is a forensic copy -- `restore` refuses a
              candidate with violations -- so the repair happens in the live
              database and the next night takes the restorable one.

  usage [--month YYYY-MM] [--json]
              The month's cost: per bill spent, estimated (`bound` rows),
              held (`reserved` and `sending`) and cap; per bill, provider
              and role; every `bound` row, so what was billed is never
              counted as nothing; the total. The Usage screen shows the
              same read. Its one write is the orphan sweep a reservation
              runs: a dead owner's `reserved` rows are released and its
              `sending` rows settle to `bound`. --json prints the read
              alone, the bytes `/api/usage` serves.
  judgments [--since STAMP] [--until STAMP] [--json]
              What the judgment-model callers did, compared by stage: calls,
              how they ended, tokens, cost, latency, the fallback rate and
              how many judgments are known to have changed anything. A read
              only. The bounds are compared as text against the one
              timestamp shape, so --since 2026-09 is a month and
              --since 2026-09-20 is a day.
  serve --loopback [--port N] [--database PATH]
              Serve the database to `sd_db.remote` connections on
              127.0.0.1 (default port 8769, 0 picks a free one). Loopback
              only until peer identity lands. Writes a fresh token to
              <database>.serve.token (mode 0600) and refuses a session
              without it. Takes <database>.serve.lock and refuses to start
              when another server owns it. Never creates a database. Logs
              each write transaction's longest gap between frames, and the
              longest of the run on exit.
              Restart it after installing sd_db: it states the build it
              loaded at start, and refuses every session while its files
              hold another one.
  test        Run the package's own tests. Builds a wheel and installs it
              into a throwaway virtual environment on the way, so the test
              that asserts the installed copy imports is a real install.
              Extra arguments go to unittest, e.g. `-k name` or `-v`.
  test --remote HOST:PORT TOKEN_FILE [ARGS]
              The same suite with every in-process `connect` going over the
              wire to a `serve --loopback` at HOST:PORT, which wrote its
              token to TOKEN_FILE. Its summary line must equal the local one.
  test --surface [ARGS]
              The same suite with every `connect` returning a connection
              that raises on any attribute outside the surface the remote
              connection carries (`sd_db.testing.surface`).
  build       Build the wheel into ./dist and print its path.
  install DIR Install the built wheel into the virtual environment at DIR.
              Never editable: the pack and this repository both install a
              copy, and an editable install would hide a missing file.
  check       What CI runs: `test` with no arguments, the whole suite.
  library     Print which `sd_db` the database verbs would run under $PYTHON
              now, `checkout` or `installed`, by the rule below. For a
              caller that imports `sd_db` itself: `local-sd-plan`'s nightly.
  help        This text.

Which library runs: the verbs that open a database (backup, serve, and the
ones `sd_db.jobs.cli` answers) run this checkout's `sd_db`, unless the Python
they run under has a copy installed that is built for a newer schema. Then
they run that copy and say so on stderr, so a checkout behind `origin/main`
still opens a database the installed copy migrated. SD_DB_LIBRARY=checkout
or SD_DB_LIBRARY=installed forces one side; `test` and `check` always run
the checkout. The other verbs build or test this checkout.

The build backend is `_build.py` in this folder and has no dependencies, so
every command above works with no network.
USAGE
}

# Failures of the store never depend on the store, so a failed backup reports
# itself by mail and not by a row. Best effort on the channel, never on the
# exit code: the job failed, and it says so whether or not the mail left.
report_failure() {
    "$NOTIFY" -t "${2:-sd-db backup FAILED}" -k status -F -c email -b "$1" || \
        echo "sd-db.sh backup: the failure mail did not leave" >&2
}

case "${1:-}" in
    ""|-h|--help|help) ;;
    *) resolve_python ;;
esac

case "${1:-}" in
    backup)
        shift
        # Three outcomes, not two. Exit 2 is the backup PASSING over a
        # source whose rows point at rows that do not exist (sd:744): the
        # snapshot is on disk and the prune ran, so mailing it as
        # "sd-db backup FAILED" would send the operator hunting for a backup
        # that is right there. It still mails and still exits nonzero,
        # because the silent version of this is the defect it was written
        # for. `$status` is read before anything else runs: `local-notify`
        # and `echo` both overwrite `$?`.
        # `|| status=$?` and not a bare assignment: `set -e` at the top of
        # this file kills the script on a failing command substitution, which
        # would swallow the output and send no mail at all.
        choose_library
        status=0
        output="$(exec_library -m sd_db.jobs.backup "$@" 2>&1)" || status=$?
        # 3 and not 2: `argparse` exits 2 on a usage error, so `backup --keep
        # nope` would reach this arm and mail a referential finding about a
        # typo. `sd_db.jobs.backup.BROKEN_SOURCE` holds the other half.
        if [ "$status" -eq 3 ]; then
            echo "$output" >&2
            report_failure "$output" "sd-db backup: the source is referentially broken"
            exit 3
        fi
        if [ "$status" -ne 0 ]; then
            echo "$output" >&2
            report_failure "$output"
            exit 1
        fi
        echo "$output"
        ;;
    init|migrate|status|restore|repo|item|work|import|verify|retire|usage|judgments)
        choose_library
        exec_library -m sd_db.jobs.cli "$@"
        ;;
    test)
        # Everything after the verb goes to unittest, as `local-sd-plan`
        # and `local-sd-runner` do. Before this the
        # branch took no `"$@"`, so `test -k name` dropped the pattern
        # silently and ran the full two-minute suite.
        shift
        default_acceptance_pack
        # The suite tests this checkout, so every verb it drives through this
        # script runs the checkout too, whatever copy $PYTHON has installed.
        SD_DB_LIBRARY=checkout
        export SD_DB_LIBRARY
        case "${1:-}" in
            --remote)
                shift
                if [ $# -lt 2 ]; then
                    echo "sd-db test --remote: needs HOST:PORT and the TOKEN_FILE of a \`serve --loopback\`" >&2
                    exit 1
                fi
                PYTHONPATH="$DIR" exec "$PYTHON" -m sd_db.testing.wire "$@"
                ;;
            --surface)
                shift
                PYTHONPATH="$DIR" exec "$PYTHON" -m sd_db.testing.surface "$@"
                ;;
        esac
        # `discover` takes options, not test ids: a leading id such as
        # `tests.test_cli.TheConventions` goes to the plain unittest form.
        case "${1:-}" in
            ""|-*) ;;
            *) PYTHONPATH="$DIR" exec "$PYTHON" -m unittest "$@" ;;
        esac
        PYTHONPATH="$DIR" exec "$PYTHON" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
        ;;
    library)
        # The choice alone, so a caller that runs `sd_db` in its own process
        # makes this one and not a copy of it (sd:1812). The note a switch
        # prints goes to stderr, as it does for every other verb.
        choose_library
        echo "$LIBRARY"
        ;;
    serve)
        shift
        choose_library
        exec_library -m sd_db.serve "$@"
        ;;
    check)
        # What CI runs, and it runs the whole suite: an argument here would
        # narrow it, so refuse one rather than drop it the way `test` used to.
        if [ $# -gt 1 ]; then
            echo "sd-db check: takes no arguments; \`test\` passes them to unittest" >&2
            exit 1
        fi
        default_acceptance_pack
        SD_DB_LIBRARY=checkout
        export SD_DB_LIBRARY
        PYTHONPATH="$DIR" exec "$PYTHON" -m unittest discover -s "$DIR/tests" -t "$DIR"
        ;;
    build)
        mkdir -p "$DIR/dist"
        PYTHONPATH="$DIR" "$PYTHON" -c 'import _build, sys; print(_build.build_wheel(sys.argv[1]))' "$DIR/dist"
        ;;
    install)
        target="${2:-}"
        if [ -z "$target" ]; then
            echo "sd-db install: needs the path of a virtual environment" >&2
            exit 1
        fi
        wheel="$("$0" build)"
        "$target/bin/python" -m pip install --no-index --force-reinstall "$DIR/dist/$wheel"
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        usage
        exit 1
        ;;
esac
