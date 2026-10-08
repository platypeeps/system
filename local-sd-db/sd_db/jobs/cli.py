"""`sd-db.sh`'s Python half: the schema verbs, the repositories, the migrations.

The shell script is the entrypoint the repository's convention asks for; it
resolves the folder and hands the verb here. Everything that needs to know
the schema lives on this side.

`import`, `verify` and `retire` are the migrations of requirement 2. `import`
and `verify` never retire anything, whatever source they are pointed at;
`retire` is the separate verb and the separate sitting, and it exists for
`docs/work` alone. The other four sources stay authoritative and stay written
by whatever writes them today, because the retire step of each source runs in
a pull request after the one that lands its writer -- criterion 23's rule,
and the reason the freeze is minutes rather than a slice.

`sd shadow sync` is deliberately **not** here. Verbs live in the pack; what
lives in this library is `sd_db.shadow_sync`, the function that verb calls.
"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path

from .. import credentials, judgment, removal, usage
from ..backup import restore as restore_snapshot
from ..database import connect, default_path, schema_version, tables
from ..errors import SdDbError
from ..migrate import initialise, migrate
from ..registry import ensure_seeded
from ..registry import read as read_registry
from ..registry import registry_path
from ..repos import absolute_under_home
from ..repos import add as add_repo
from ..repos import registered
from ..repos import registered_for
from ..repos import ConfMissing, seed as seed_repos
from ..repos import set_ci, set_managed, set_runner_merge, set_satellite_gate
from ..schema import SCHEMA_VERSION
from ..sources import docs_work, index_cache, issues, register
from ..sources import retire as retire_source
from ..sources import run as run_sitting
from ..sources import vault
from ..sources import verify as verify_source
from ..sources.frontmatter import FrontmatterError
from ..sources.frontmatter import read as read_frontmatter
from ..workflow import WorkflowError, register_work_item
from ..writes import OPEN_STATE_KINDS


def _home() -> Path:
    return Path(os.environ.get("HOME", "~")).expanduser()


def _seed_registry(home: Path) -> str:
    """Seed `provider` and `bill` from the file, if there is a file.

    A machine with no registry yet is not an error at `init`: the file
    arrives with the pack, and `registry.read` seeds the tables on the first
    read through a writable connection that finds it -- a dashboard action,
    or this verb run again. The message names that, because it used to
    promise "on first read" while no read seeded anything.
    """
    path = registry_path(home)
    if not path.exists():
        return (
            f"no {path.name} yet; provider and bill seed on the first read "
            f"through a writable connection once it exists (a dashboard "
            f"action, or `sd-db.sh init` again)"
        )
    connection = connect(home=home)
    try:
        registry = read_registry(path, connection=None)
        written = ensure_seeded(connection, registry)
    finally:
        connection.close()
    if written == 0:
        return f"provider and bill already seeded from {path}; nothing written"
    return f"seeded {written} row(s) from {path}"


def command_init(_argv: list[str]) -> int:
    home = _home()
    result = initialise(home=home)
    print(f"sd-db: {result.path} at schema version {result.after}")
    if result.applied:
        print(f"sd-db: applied migration(s) {result.applied}")
    print(f"sd-db: {_seed_registry(home)}")
    return 0


def command_migrate(_argv: list[str]) -> int:
    home = _home()
    result = migrate(home=home)
    if not result.applied:
        print(f"sd-db: already at schema version {result.after}; nothing applied")
        return 0
    print(
        f"sd-db: {result.path} {result.before} -> {result.after}, "
        f"applied {result.applied}"
    )
    return 0


def command_status(_argv: list[str]) -> int:
    home = _home()
    path = default_path(home)
    if not path.exists():
        print(f"sd-db: no database at {path}; run `sd-db.sh init`")
        return 1
    # Read-only, so a database awaiting a migration still answers.
    connection = connect(path, write=False)
    try:
        found = schema_version(connection)
        print(f"sd-db: {path}")
        print(f"sd-db: schema version {found}, this library is built for {SCHEMA_VERSION}")
        print(f"sd-db: {len(tables(connection))} table(s)")
        # One line per kind: the hub holds tens of thousands of checkpoint
        # and heartbeat rows, and their NULL resolved_at is not open (sd:2849).
        marks = ", ".join("?" * len(OPEN_STATE_KINDS))
        for row in connection.execute(
            f"SELECT kind, COUNT(*) AS rows FROM state WHERE resolved_at IS NULL "
            f"AND kind NOT IN ({marks}) GROUP BY kind ORDER BY kind", OPEN_STATE_KINDS
        ):
            print(f"sd-db: {row['rows']} {row['kind']} row(s) without resolved_at (a log; not open work)")
        for row in connection.execute(
            f"SELECT kind, key, timestamp FROM state WHERE resolved_at IS NULL "
            f"AND kind IN ({marks}) ORDER BY timestamp", OPEN_STATE_KINDS
        ):
            print(f"sd-db: unresolved {row['kind']} {row['key'] or ''} at {row['timestamp']}")
        # sd:1439: the key columns hold `~/` for a path under this home.
        counts = absolute_under_home(connection)
        print(f"sd-db: {sum(counts.values())} key value(s) absolute under this home")
        for name, count in counts.items():
            if count:
                print(f"sd-db:   {name}: {count}")
    finally:
        connection.close()
    return 0


def command_restore(argv: list[str]) -> int:
    if not argv:
        print("sd-db restore: needs a dated directory", file=sys.stderr)
        return 1
    target = restore_snapshot(argv[0], home=_home())
    print(f"sd-db: restored {argv[0]} to {target}")
    print("sd-db: the restore is unreconciled; `sd restore resume` clears it")
    return 0



def command_usage(argv: list[str]) -> int:
    """`usage [--month YYYY-MM] [--json]`: the month, after the orphan sweep.

    The sweep is the one write, so the connection is a writer; `--json`
    prints `usage.json_text` and nothing else, and what the sweep did goes
    to stderr, so the JSON is the screen's bytes on the same store.
    """
    month, as_json = None, False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--json":
            as_json = True
        elif flag == "--month" and rest:
            month = rest.pop(0)
        else:
            print("sd-db usage: takes --month YYYY-MM and --json", file=sys.stderr)
            return 1
    connection = _open_for_write()
    try:
        found, released = usage.report(connection, month=month)
    finally:
        connection.close()
    if released.deleted or released.bound:
        print(f"sd-db usage: released {len(released.deleted)} reserved row(s) and bound "
              f"{len(released.bound)} sending row(s) of dead owners", file=sys.stderr)
    sys.stdout.write(usage.json_text(found) if as_json else usage.text(found))
    return 0


def command_credentials(argv: list[str]) -> int:
    """`credentials`: probe credential presence and expiry and record the
    heartbeat Health reads (sd:2203). One line per probe; never a value."""
    if argv:
        print("sd-db credentials: takes no arguments", file=sys.stderr)
        return 1
    connection = _open_for_write()
    try:
        body = credentials.check(connection, env=os.environ)
    finally:
        connection.close()
    for probe in body["probes"]:
        print(credentials.describe(probe))
    return 0


def command_judgments(argv: list[str]) -> int:
    """`judgments [--since STAMP] [--until STAMP] [--json]`: the per-stage
    comparison of calls, cost, latency, fallbacks and overrides.

    A read and nothing else, so the connection is a reader: a machine halfway
    through an upgrade can still be asked what its callers spent. The bounds
    are compared as text against the one timestamp shape, so `--since 2026-09`
    is a month and `--since 2026-09-20` is a day.
    """
    if argv and argv[0] == "label":
        return _judgments_label(argv[1:])
    if argv and argv[0] == "unlabelled":
        return _judgments_unlabelled(argv[1:])
    if argv and argv[0] == "compare":
        return _judgments_compare(argv[1:])
    since, until, as_json = None, None, False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--json":
            as_json = True
        elif flag == "--since" and rest:
            since = rest.pop(0)
        elif flag == "--until" and rest:
            until = rest.pop(0)
        else:
            print("sd-db judgments: takes --since STAMP, --until STAMP and --json, "
                  "or `label`, `unlabelled` or `compare`", file=sys.stderr)
            return 1
    connection = _open_for_read()
    try:
        rows = judgment.by_stage(connection, since=since, until=until)
    finally:
        connection.close()
    sys.stdout.write(judgment.json_text(rows) if as_json else judgment.text(rows))
    return 0


def _judgments_compare(argv: list[str]) -> int:
    """`judgments compare [--stage NAME] [--since STAMP] [--until STAMP] [--json]`.

    The comparison arms of sd:2366 against the Jev row of the same pair:
    calls, latency percentiles, tokens, cost, agreement, and accuracy and
    Brier on labelled pairs. A read only.
    """
    options: dict[str, str | None] = {"stage": None, "since": None, "until": None}
    as_json = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--json":
            as_json = True
        elif flag in ("--stage", "--since", "--until") and rest:
            options[flag[2:]] = rest.pop(0)
        else:
            print("sd-db judgments compare: takes --stage NAME, --since STAMP, "
                  "--until STAMP and --json", file=sys.stderr)
            return 1
    connection = _open_for_read()
    try:
        report = judgment.compare(connection, **options)
    finally:
        connection.close()
    sys.stdout.write(judgment.compare_json(report) if as_json
                     else judgment.compare_text(report))
    return 0


def _judgments_label(argv: list[str]) -> int:
    """`judgments label --row N --override NUMBER --source NAME [--replace]`.

    The one write path for a label, so a labeller never opens the database
    itself. The ledger refuses a row that does not exist, an override that is
    not a number, a source that is not an identifier, and a different label
    over an existing one without `--replace`; each refusal exits 1 naming it.
    """
    usage_line = ("sd-db judgments label: takes --row N --override NUMBER "
                  "--source NAME [--replace]")
    options: dict[str, str] = {}
    replace = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--replace":
            replace = True
        elif flag in ("--row", "--override", "--source") and rest:
            options[flag[2:]] = rest.pop(0)
        else:
            print(usage_line, file=sys.stderr)
            return 1
    if set(options) != {"row", "override", "source"} or not options["row"].isdigit():
        print(usage_line, file=sys.stderr)
        return 1
    row_id = int(options["row"])
    connection = _open_for_write()
    try:
        changed = judgment.label(connection, row_id, options["override"],
                                 options["source"], replace=replace)
    finally:
        connection.close()
    if changed:
        print(f"sd-db: labelled row {row_id} {options['override']} "
              f"({options['source']})")
    else:
        print(f"sd-db: row {row_id} already labelled {options['override']}; unchanged")
    return 0


def _judgments_unlabelled(argv: list[str]) -> int:
    """`judgments unlabelled --stage NAME [--prefix TEXT] [--json]`: the
    decisions of one stage that carry no label, oldest first. A read."""
    stage, prefix, as_json = None, None, False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--json":
            as_json = True
        elif flag == "--stage" and rest:
            stage = rest.pop(0)
        elif flag == "--prefix" and rest:
            prefix = rest.pop(0)
        else:
            stage = None
            break
    if not stage:
        print("sd-db judgments unlabelled: takes --stage NAME [--prefix TEXT] [--json]",
              file=sys.stderr)
        return 1
    connection = _open_for_read()
    try:
        rows = judgment.unlabelled(connection, stage, prefix=prefix)
    finally:
        connection.close()
    if as_json:
        sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
        return 0
    if not rows:
        print(f"sd-db: no unlabelled {stage} rows")
    for row in rows:
        print(f"{row['id']}  {row['timestamp']}  {row['arm']}  "
              f"{row['question_id'] or '-'}  answer {row['answer'] or '-'}  "
              f"confidence {'-' if row['confidence'] is None else row['confidence']}")
    return 0


#: The five migrations of requirement 2, by the name the operator types.
#: Enumerated here rather than discovered by scanning the package: a source
#: is a deliberate addition with a decision behind it, and import-time
#: discovery would let one appear because a file landed in a directory.
SOURCES = {
    "index": lambda connection: index_cache.Reader.at(),
    "docs-work": docs_work.Reader.from_table,
    "register": lambda connection: register.Reader.at(),
    "vault": lambda connection: vault.Reader.at(),
    "issues": lambda connection: issues.Reader(),
}


def _open_for_write() -> sqlite3.Connection:
    home = _home()
    path = default_path(home)
    if not path.exists():
        raise SdDbError(f"no database at {path}; run `sd-db.sh init`")
    return connect(path)


def _open_for_read() -> sqlite3.Connection:
    """`mode=ro` and `query_only`: a preview changes nothing, not even the journal mode (Copilot on #414)."""
    home = _home()
    path = default_path(home)
    if not path.exists():
        raise SdDbError(f"no database at {path}; run `sd-db.sh init`")
    return connect(path, write=False)


def command_repo(argv: list[str]) -> int:
    """`repo add <path>`, `repo seed [conf]`, `repo list [--managed]`,
    `repo runner-merge <path> <manual|auto>`, `repo managed <path> <yes|no>`,
    `repo ci <path> <github|local>`, `repo satellite-gate <path> <off|accept>`,
    `repo remove <path> ...`.

    The `repo` table is what criterion 6 enumerates from, so an empty one
    makes that criterion pass over nothing. `add` and `seed` are the two ways
    a row gets into it and there is no third; `remove` is the one way a row
    leaves (sd:754, `_remove`), and `list` reads.

    `runner-merge` writes the one column on that row nothing could set
    (sd:1131). It changes no other field, so it is a write to a row the two
    registering verbs already made. `managed` is the same shape for
    `repo.managed` (sd:1619). Nothing derives that flag; the operator sets
    each row. `ci` is the same shape again for `repo.ci` (sd:1843): whether
    the pack waits for GitHub Actions or runs `sd-check` locally.
    `satellite-gate` is that shape for `repo.satellite_gate` (sd:2704):
    whether the hub may merge on a satellite's gate pass.

    `list` prints `managed`, `ci`, `satellite_gate`, then `runner_merge`, so
    the merge setting stays the last field of each row, where the operator's
    instructions read it.
    """
    verbs = ("add", "seed", "list", "runner-merge", "managed", "ci", "satellite-gate", "remove")
    if not argv or argv[0] not in verbs:
        print(f"sd-db repo: expected {', '.join(verbs[:-1])} or {verbs[-1]}",
              file=sys.stderr)
        return 1
    if argv[0] == "remove":
        return _remove("repo", argv[1:])
    verb, rest = argv[0], argv[1:]
    connection = _open_for_write()
    try:
        if verb == "add":
            if not rest:
                print("sd-db repo add: needs a path", file=sys.stderr)
                return 1
            path = add_repo(connection, rest[0], home=_home())
            print(f"sd-db: registered {path}")
            return 0
        if verb == "seed":
            try:
                result = seed_repos(connection, rest[0] if rest else None, home=_home())
            except ConfMissing as error:
                print(f"sd-db repo seed: {error}", file=sys.stderr)
                return 1
            print(
                f"sd-db: registered {len(result.registered)} repositor(ies); "
                f"{len(result.absent)} named by the conf are not cloned here"
            )
            for slug in result.absent:
                print(f"sd-db:   not cloned: {slug}")
            return 0
        if verb == "runner-merge":
            if len(rest) != 2:
                print("sd-db repo runner-merge: needs a path and manual or auto",
                      file=sys.stderr)
                return 1
            path, before = set_runner_merge(connection, rest[0], rest[1])
            print(f"sd-db: {path} runner_merge {before} -> {rest[1]}")
            return 0
        if verb == "managed":
            if len(rest) != 2:
                print("sd-db repo managed: needs a path and yes or no",
                      file=sys.stderr)
                return 1
            path, before = set_managed(connection, rest[0], rest[1])
            print(f"sd-db: {path} managed {before} -> {rest[1]}")
            return 0
        if verb == "ci":
            if len(rest) != 2:
                print("sd-db repo ci: needs a path and github or local",
                      file=sys.stderr)
                return 1
            path, before = set_ci(connection, rest[0], rest[1])
            print(f"sd-db: {path} ci {before} -> {rest[1]}")
            return 0
        if verb == "satellite-gate":
            if len(rest) != 2:
                print("sd-db repo satellite-gate: needs a path and off or accept",
                      file=sys.stderr)
                return 1
            path, before = set_satellite_gate(connection, rest[0], rest[1])
            print(f"sd-db: {path} satellite_gate {before} -> {rest[1]}")
            return 0
        if rest not in ([], ["--managed"]):
            print(f"sd-db repo list: unknown argument {' '.join(rest)}; "
                  f"expected nothing or --managed", file=sys.stderr)
            return 1
        rows = registered(connection)
        if not rows:
            print("sd-db: the `repo` table is empty; run `sd-db.sh repo seed`")
            return 1
        if rest:
            rows = [row for row in rows if row["managed"]]
            if not rows:
                # An empty answer is an answer: a caller asking which
                # repositories are managed gets none, not a failure.
                print("sd-db: no repository is marked managed; run "
                      "`sd-db.sh repo managed PATH yes`", file=sys.stderr)
                return 0
        for row in rows:
            print(f"sd-db: {row['path']}  {row['remote'] or '-'}  "
                  f"{row['status_source']}  {'yes' if row['managed'] else 'no'}  "
                  f"{row['ci']}  {row['satellite_gate']}  {row['runner_merge']}")
        return 0
    finally:
        connection.close()


def command_work(argv: list[str]) -> int:
    """`work register <docs/work/<item>/prd.md>`.

    The step retirement removed. `import docs-work` reads the file source, and
    every repository has retired it, so the importer refuses on the first one
    it meets: a folder created after the cutover has no row, and no readable
    status anywhere. This makes the row, from what the folder and git already
    say.

    It takes no repository argument. The repository is the one enclosing the
    working directory (R10-D6), and the path is relative to it -- a row whose
    path resolved against a checkout the caller was not standing in would name
    a file nobody can read.
    """
    if len(argv) != 2 or argv[0] != "register":
        print("sd-db work: expected `register <docs/work/<item>/prd.md>`", file=sys.stderr)
        return 1
    root = _repository_root()
    relative = argv[1]
    prd = (root / relative).resolve()
    try:
        prd.relative_to(root)
    except ValueError:
        print(f"sd-db work register: {relative} is outside {root}", file=sys.stderr)
        return 1
    if not prd.is_file():
        print(f"sd-db work register: no file at {prd}", file=sys.stderr)
        return 1
    try:
        front, _ = read_frontmatter(prd.read_text(encoding="utf-8"))
    except (FrontmatterError, OSError, UnicodeDecodeError) as failure:
        print(f"sd-db work register: {prd}: {failure}", file=sys.stderr)
        return 1
    title, created = front.get("title"), front.get("created")
    if not title or not created:
        print(
            f"sd-db work register: {prd} needs `title:` and `created:` in its frontmatter",
            file=sys.stderr,
        )
        return 1

    branch = _working_branch(root)
    commit = _last_commit(root, prd.relative_to(root).as_posix())
    connection = _open_for_write()
    try:
        # The repository this checkout *is*, not the directory it sits in: a
        # runner clone carries the same files at a different path, and
        # resolving by path alone refuses every run the runner makes.
        repo = registered_for(connection, str(root), _origin(root))
        state = register_work_item(
            connection, repo=repo, path=prd.relative_to(root).as_posix(),
            title=str(title), created_at=str(created), branch=branch,
            source_commit=commit, who=os.environ.get("SD_SESSION") or getpass.getuser(),
        )
    except WorkflowError as failure:
        print(f"sd-db work register: {failure}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    row = state["item"]
    if not state["created"]:
        print(f"sd-db: already registered as #{row['id']} ({row['status']})")
        return 0
    print(f"sd-db: registered #{row['id']} {row['title']!r} as {row['status']}")
    if commit is None:
        print("sd-db: the file is not committed yet, so the row records no source commit")
    return 0


def _working_branch(root: Path) -> str | None:
    """The branch the row is to be worked on, or None when this checkout cannot name one.

    `item.branch` is read one way everywhere: as the branch to do the work
    on. `runner.py:_item` refuses a row without one, `configure_item` refuses
    the remote default for it, `sd_plan.py` checks it out and pushes to it.
    So what goes here is a local head, or nothing.

    Until sd:462 this wrote `docs_work.default_branch(root)` -- `origin/main`,
    a remote-tracking name that passes the runner's shape check and names no
    head, so a row carrying it read as runnable everywhere and failed only
    inside the clone. Sixty-five rows carried it. `default_branch` still means
    what its docstring says, the branch a merge lands on, for the file source
    that reads every branch of the remote; it was the wrong source for this
    column, not a wrong function.

    One case can name a working branch honestly: the checkout is on a local
    branch that is not the default, as a runner clone on `plan/<slug>` is when
    `sd-plan` registers the folder it just wrote. On the default, or detached,
    the answer is NULL -- the state a task row starts in, and the one
    `sd runner prepare --branch` exists to fill.
    """
    code, out, _ = docs_work._git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    current = out.strip() if code == 0 else ""
    if not current:
        return None
    # `origin/main` -> `main`. When origin/HEAD is unreadable -- a repository
    # without a remote -- `default_branch` can only guess `origin/main`, and
    # the guess is widened to `main` and `master`, the two names the fleet's
    # defaults go by and the pair `progress.py` accepts there. When it was
    # read, the read name alone is the default: a local `main` in a repository
    # whose default is `dev` is a working branch by this function's own
    # definition, and the unconditional pair discarded it (sd:653). The read
    # is `default_branch`'s own, so the two cannot disagree about which case
    # this is; its return value alone cannot say, since a read `origin/main`
    # and the guess are the same string.
    _, _, default = docs_work.default_branch(root).partition("/")
    code, out, _ = docs_work._git(
        root, "for-each-ref", "--format=%(symref:short)", "refs/remotes/origin/HEAD"
    )
    read = code == 0 and bool(out.strip())
    if current in ({default} if read else {default, "main", "master"}):
        return None
    return current


def _origin(root: Path) -> str | None:
    """This checkout's `origin` URL, or None when it has no remote at all."""
    code, out, _ = docs_work._git(root, "remote", "get-url", "origin")
    return (out.strip() or None) if code == 0 else None


def _repository_root() -> Path:
    code, out, err = docs_work._git(Path.cwd(), "rev-parse", "--show-toplevel")
    if code != 0:
        raise SdDbError(f"not inside a git repository: {(err or out).strip()}")
    return Path(out.strip()).resolve()


def _last_commit(root: Path, relative: str) -> str | None:
    """The newest commit touching the file, or None while it is uncommitted.

    Uncommitted includes edited: `log -1` reads history only, so a tracked
    file with working-tree changes would be recorded against a commit that
    does not carry the contents being registered. A git read that fails is
    raised, not taken for uncommitted, or a broken checkout writes NULL.

    The same commit the file source recorded -- `docs_work._candidate` reads
    `log -1` over the path -- so a row made here and a row made by the old
    import name the same thing by `source_commit` and a reader comparing them
    is comparing like with like.

    It is read against `HEAD`, not against `origin/main` as the import was: a
    folder is registered while it is being written, on the branch that carries
    it, and the default branch has not seen it yet. Uncommitted is the
    ordinary case at that moment, which is why the column is nullable.
    """
    code, out, err = docs_work._git(root, "status", "--porcelain", "--", relative)
    if code != 0:
        raise SdDbError(f"cannot read the status of {relative}: {(err or out).strip()}")
    if out.strip():
        return None
    code, out, err = docs_work._git(root, "log", "-1", "--format=%H", "--", relative)
    if code != 0:
        raise SdDbError(f"cannot read the history of {relative}: {(err or out).strip()}")
    return out.strip() or None


def _source(name: str, connection):
    if name not in SOURCES:
        raise SdDbError(
            f"no source {name!r}; the five are {', '.join(sorted(SOURCES))}"
        )
    return SOURCES[name](connection)


def _for_import(source):
    """The source narrowed to what the file still answers for, if it can narrow.

    Asked of the source rather than switched on its name, as `retire` asks
    for `for_retire`: `docs/work` is the one source with repositories that
    have retired, and this holds no list of sources to keep in step.
    """
    narrow = getattr(source, "for_import", None)
    return source if narrow is None else narrow()


def _retired(token: str, verb: str, source) -> tuple[list[str], bool]:
    """One line per repository the run left out as retired, and whether that
    was all of them.

    A retired repository is the intended end state of `retire`, not a fault
    of `import`: its status lives in the row and the row is what to read. So
    it is reported by name, exit code untouched, and a fleet with nothing
    left on the file source is said plainly rather than refused -- which is
    what every run of both verbs did from the last retire until 2026-09-11,
    on every one of the twelve registered repositories.
    """
    already = list(getattr(source, "already", []))
    lines = [
        f"{token}: {path} was retired and its status lives in the `item` row "
        f"now; read the row, not the file"
        for path in already
    ]
    nothing_left = getattr(source, "nothing_left", None)
    if already and nothing_left is not None and nothing_left():
        lines.append(
            f"{token}: {len(already)} repositor{'y' if len(already) == 1 else 'ies'}, "
            f"all retired; nothing to {verb} -- use `sd-db.sh work register` "
            f"for a new item"
        )
        return lines, True
    return lines, False


def command_import(argv: list[str]) -> int:
    """Freeze, import and verify one source. Retires nothing, and says so."""
    if not argv:
        print(
            f"sd-db import: needs one of {', '.join(sorted(SOURCES))}",
            file=sys.stderr,
        )
        return 1
    connection = _open_for_write()
    try:
        source = _for_import(_source(argv[0], connection))
        retired, nothing_left = _retired(argv[0], "import", source)
        # Before the sitting: a failure in a repository still on `file`
        # leaves this function, and the retired ones must be named anyway.
        for line in retired:
            print(f"sd-db: {line}")
        sitting = None if nothing_left else run_sitting(connection, source)
    finally:
        connection.close()
    if sitting is None:
        return 0
    for line in sitting.report():
        print(f"sd-db: {line}")
    # The old line here said "nothing retired; the source is unchanged and
    # still authoritative" for every source. It is still true of four of
    # them, and false of `docs/work`, which now has a retire step and a verb
    # to run it. A comment that outlives what it describes is the defect this
    # pair of work items keeps finding, and so is a printed line.
    if hasattr(source, "retire"):
        print(
            f"sd-db: nothing retired here; `sd-db.sh retire {argv[0]}` is the "
            f"sitting that does, and it runs this import again first"
        )
    else:
        print("sd-db: nothing retired; the source is unchanged and still authoritative")
    return 0 if sitting.clean else 1


def command_retire(argv: list[str]) -> int:
    """The retire sitting: refuse, import, verify, snapshot, the row, the commit.

    One command, one source, one sitting, and every refusal it can make names
    what it refused on. It is not idempotent in the way `import` is -- it is
    idempotent in the way a migration is, which is that a second run finds
    the marker committed and reports that there was nothing left to do.
    """
    if not argv:
        print(
            f"sd-db retire: needs one of {', '.join(sorted(SOURCES))}",
            file=sys.stderr,
        )
        return 1
    connection = _open_for_write()
    try:
        result = retire_source(
            connection, _source(argv[0], connection), token=argv[0], home=_home()
        )
    finally:
        connection.close()
    for line in result.report():
        print(f"sd-db: {line}")
    return 0


def command_verify(argv: list[str]) -> int:
    """Compare one source with the rows, by identity and content, never by count."""
    if not argv:
        print(
            f"sd-db verify: needs one of {', '.join(sorted(SOURCES))}",
            file=sys.stderr,
        )
        return 1
    connection = _open_for_write()
    try:
        source = _for_import(_source(argv[0], connection))
        retired, nothing_left = _retired(argv[0], "verify", source)
        # Before the freeze, for the reason `command_import` gives.
        for line in retired:
            print(f"sd-db: {line}")
        if nothing_left:
            frozen, differences = None, []
        else:
            frozen = source.freeze()
            differences = verify_source(connection, source, frozen)
    finally:
        connection.close()
    if frozen is None:
        return 0
    if not differences:
        print(f"sd-db: {argv[0]}: {len(frozen.records)} record(s) agree with the rows")
        return 0
    print(f"sd-db: {argv[0]}: {len(differences)} difference(s)", file=sys.stderr)
    for difference in differences:
        print(f"sd-db:   {difference}", file=sys.stderr)
    return 1


def command_item(argv: list[str]) -> int:
    """`item remove ID --who NAME --reason TEXT [--apply --if-fingerprint HEX]` (`design.md` section 6)."""
    if not argv or argv[0] != "remove":
        print("sd-db item: expected remove", file=sys.stderr)
        return 1
    return _remove("item", argv[1:])


#: The options `item remove` and `repo remove` take, and whether each takes a value.
REMOVE_OPTIONS = {"--who": True, "--reason": True, "--if-fingerprint": True, "--apply": False, "--with-items": False}


def _remove_options(kind: str, argv: list[str]) -> dict:
    """The target and the options, refused as `SdDbError` before the store is opened.

    `--who` and `--reason` are required for the preview too: the fingerprint
    does not cover them, and the preview's last line is the exact apply
    command. Nothing falls back to `getpass.getuser()` for `who` (note 1846,
    Q9). `--apply` needs `--if-fingerprint`, as `recovery.reimport` does.
    """
    verb = f"sd-db.sh {kind} remove"
    options = {"--with-items": False, "--apply": False}
    target = None
    rest = list(argv)
    while rest:
        word = rest.pop(0)
        if word in REMOVE_OPTIONS:
            if word == "--with-items" and kind != "repo":
                raise SdDbError(f"{verb}: --with-items belongs to repo remove")
            if not REMOVE_OPTIONS[word]:
                options[word] = True
            elif rest:
                options[word] = rest.pop(0)
            else:
                raise SdDbError(f"{verb}: {word} needs a value")
        elif word.startswith("--"):
            raise SdDbError(f"{verb}: unknown option {word}")
        elif target is None:
            target = word
        else:
            raise SdDbError(f"{verb}: one {'ID' if kind == 'item' else 'PATH'}, not {target} and {word}")
    if target is None:
        raise SdDbError(f"{verb}: needs the {'item ID' if kind == 'item' else 'repository PATH'}")
    if kind == "item":
        try:
            target = int(target)
        except ValueError:
            raise SdDbError(f"{verb}: {target} is not an item id") from None
    for name in ("--who", "--reason"):
        if name not in options:
            raise SdDbError(f"{verb}: {name} is required, in the preview too; nothing is taken from the login")
    # The preview's last line is the apply command, and `shlex.quote` keeps
    # a line break inside its quotes (Copilot on #414): one line each.
    for name, value in (("--who", options["--who"]), ("--reason", options["--reason"]), ("the target", str(target))):
        if "\n" in value or "\r" in value:
            raise SdDbError(f"{verb}: {name} must be one line, so the apply command is one line")
    if options["--apply"] and "--if-fingerprint" not in options:
        raise SdDbError(f"{verb}: --apply needs --if-fingerprint HEX, the fingerprint the preview printed")
    if "--if-fingerprint" in options and not options["--apply"]:
        raise SdDbError(f"{verb}: --if-fingerprint goes with --apply")
    return {"target": target, "who": options["--who"], "reason": options["--reason"],
            "with_items": options["--with-items"], "apply": options["--apply"],
            "fingerprint": options.get("--if-fingerprint")}


def _apply_command(kind: str, target, options: dict, fingerprint: str) -> str:
    words = ["sd-db.sh", kind, "remove", str(target)]
    if options["with_items"]:
        words.append("--with-items")
    words += ["--who", options["who"], "--reason", options["reason"], "--apply", "--if-fingerprint", fingerprint]
    return " ".join(shlex.quote(word) for word in words)


def _print_preview(kind: str, options: dict, plan: dict) -> int:
    """The plan as the operator reads it; its last line is the apply command. Exit 3 on any refusal."""
    target = options["target"]
    print(f"sd-db: remove {kind} {target}" + (" --with-items" if options["with_items"] else ""))
    print(f"sd-db: rows: {sum(plan['counts'].values())} in {plan['record']['notes']} note(s)")
    print("sd-db: removed:")
    for entry in plan["rows"]:
        print(f"sd-db:   {entry['table']} {removal._key(entry['table'], entry['row'])}")
    if plan.get("detach"):
        # Runs of items moved to another repo stay, with no repo (sd:2581).
        print("sd-db: detached, kept with their item:")
        for entry in plan["detach"]:
            print(f"sd-db:   runner_run {entry['run']} of item {entry['item']}, now on {entry['item_repo']}")
    if plan["refusals"]:
        print("sd-db: refused:")
        for refusal in plan["refusals"]:
            print(f"sd-db:   {refusal['code']} {refusal['table']} {refusal['key']}: {refusal['message']}")
            for line in refusal["commands"]:
                print(line)
    if plan["move"]["files"]:
        print("sd-db: move after commit:")
        for path in plan["move"]["files"]:
            print(f"sd-db:   file {path}")
        print(f"sd-db:   to {plan['move']['to']}")
    if plan["left"]:
        print("sd-db: left:")
        for line in plan["left"]:
            print(f"sd-db:   {line}")
    for warning in plan["warnings"]:
        print(f"sd-db: warning: {warning}")
    print(f"sd-db: fingerprint: {plan['fingerprint']}")
    if plan["refusals"]:
        print(f"sd-db: preview only; {len(plan['refusals'])} refusal(s) hold the apply until they clear")
    else:
        print("sd-db: preview only; nothing was removed. To apply, run the next line as it is:")
    print(_apply_command(kind, target, options, plan["fingerprint"]))
    return 3 if plan["refusals"] else 0


def _print_apply(result: dict) -> int:
    """What the apply did. Exit 4 when the rows are gone but the move stopped: the lines finish it."""
    counts = ", ".join(f"{count} {table}" for table, count in result["removed"].items())
    print(f"sd-db: removed {counts}")
    for run in result.get("detached", []):
        print(f"sd-db: detached runner_run {run}")
    print(f"sd-db: record item {result['record']} in {result['notes']} note(s); backup {result['backup']}")
    for path in result["moved"]:
        print(f"sd-db: moved {path}")
    if result["move_error"] is None:
        return 0
    print(f"sd-db: the rows are gone and the record is filed, but the journal move stopped: "
          f"{result['move_error']}", file=sys.stderr)
    if result["move_commands"]:
        print("sd-db: finish the move with these lines, in order:", file=sys.stderr)
        for line in result["move_commands"]:
            print(line)
    return 4


def _remove(kind: str, argv: list[str]) -> int:
    """Both verbs: the options, G4, the preview, and with `--apply` the apply under `signal_stop`."""
    options = _remove_options(kind, argv)
    program = f"sd-db.sh {kind} remove"
    principal = getpass.getuser()
    session = os.environ.get("SD_SESSION")
    removal.check_actor(who=options["who"], reason=options["reason"], session=session, principal=principal,
                        program=program)
    home = _home()
    connection = _open_for_write() if options["apply"] else _open_for_read()
    try:
        if not options["apply"]:
            if kind == "item":
                plan = removal.plan_item(connection, options["target"], home=home)
            else:
                plan = removal.plan_repo(connection, options["target"], with_items=options["with_items"], home=home)
            return _print_preview(kind, options, plan)
        with removal.signal_stop() as stop:
            result = removal.apply(connection, kind, options["target"], fingerprint=options["fingerprint"],
                                   who=options["who"], reason=options["reason"], principal=principal,
                                   program=program, home=home, with_items=options["with_items"], session=session,
                                   stop=stop)
            return _print_apply(result)
    finally:
        connection.close()


COMMANDS = {
    "init": command_init,
    "migrate": command_migrate,
    "status": command_status,
    "restore": command_restore,
    "repo": command_repo,
    "item": command_item,
    "import": command_import,
    "verify": command_verify,
    "retire": command_retire,
    "work": command_work,
    "usage": command_usage,
    "judgments": command_judgments,
    "credentials": command_credentials,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(f"sd-db: expected one of {', '.join(sorted(COMMANDS))}", file=sys.stderr)
        return 1
    try:
        return COMMANDS[argv[0]](argv[1:])
    except SdDbError as failure:
        print(f"sd-db: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
