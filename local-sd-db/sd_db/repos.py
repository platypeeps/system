"""Which repositories the system knows about, and how they get into the table.

Criterion 6 enumerates `docs/work/*/prd.md` across "every repository the
`repo` table holds". Nothing in requirement 2 said how a row gets into that
table, and an unpopulated table makes the criterion pass over nothing: zero
repositories, zero files, zero rows with no file. A criterion that passes
vacuously is worse than one that fails, because it reports a green.

So the table is populated two ways, and only two:

* **By hand**, `sd-db.sh repo add <path>`. One repository, deliberately, with
  its remote read from the checkout rather than typed.
* **From repo-sync's `repos.common.conf` and `repos.<profile>.conf`**,
  `sd-db.sh repo seed`. That pair lives in `<config>/repo-sync/` (see
  `config.py`) and is what `repo-sync` clones on this machine's profile,
  one `<subdir> <owner/repo>` per line, and it is maintained because
  the sync breaks when it is wrong. Deriving from it beats a second list that
  only drifts. Reading the personal file alone missed every entry that lives
  in common.

Seeding registers only checkouts that exist on disk. A line naming a
repository this machine has not cloned is not an error -- the file is shared
across machines -- and registering it would put a row in the table for a
path nothing can enumerate, which criterion 6 counts as a row with no file.

**The table is the bound, not the disk.** Item D's runner clones a registered
repository's whole working tree into the worktrees directory, so a clone
carries its own `docs/work/*/prd.md` files. Those files are on the machine
and have no `item` row. They are never enumerated because enumeration reads
this table and a clone was never added to it -- there is no exclusion list to
keep in step. `add` refuses a path under the worktrees directory anyway, so
an operator cannot put one there by hand and discover the consequence at the
next sitting.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, paths
from .errors import SdDbError
from .writes import upsert_repo

#: `<subdir> <owner/repo>`, with `#` comments and blank lines. The format is
#: the first line of the file itself, and this is the whole grammar.
CONF_LINE = re.compile(r"^(?P<group>[A-Za-z0-9._-]+)\s+(?P<slug>[^\s/]+/[^\s/]+)\s*$")

#: The config folder `repo-sync` reads its lists from, under the config root.
CONF_TOOL = "repo-sync"

#: The profile `repo-sync` falls back to, and the one read here unless
#: `REPO_SYNC_PROFILE` names another.
DEFAULT_PROFILE = "personal"

#: The file `repo-sync` layers under every profile but terra. It sits next to
#: the profile conf, and is found from it rather than from here.
COMMON_CONF_NAME = "repos.common.conf"

#: Where item D's runner puts a clone. Not read to exclude anything -- the
#: `repo` table is the bound -- only to refuse registering one.
WORKTREES_RELATIVE = Path(".local/share/sd/worktrees")


class RepoRefusal(SdDbError):
    """A path that cannot be a registered repository, and why."""


class ConfMissing(FileNotFoundError):
    """The profile conf a default seed reads is not in the config folder.

    A `FileNotFoundError`, so a caller that already treats an absent conf as
    "nothing to seed" keeps doing so; the message names the remedy.
    """


@dataclass(frozen=True)
class Checkout:
    """One line of the conf, resolved against the disk."""

    group: str
    slug: str
    path: Path
    present: bool

    @property
    def remote(self) -> str:
        return f"https://github.com/{self.slug}"


def repo_root(environ: dict[str, str] | None = None) -> Path:
    """Where the checkouts live. `SD_REPO_ROOT`, else `~/repos`.

    The same variable the pack's dashboard reads, spelled the same way, so a
    machine that moved its checkouts moves both at once.
    """
    env = os.environ if environ is None else environ
    return Path(os.path.expanduser(env.get("SD_REPO_ROOT") or "~/repos"))


def worktrees_root(home: Path | str | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME", "~")).expanduser()
    return base / WORKTREES_RELATIVE


def profile(environ: dict[str, str] | None = None) -> str:
    """`REPO_SYNC_PROFILE`, else `personal`: the list `repo-sync` clones here."""
    env = os.environ if environ is None else environ
    return env.get("REPO_SYNC_PROFILE") or DEFAULT_PROFILE


def conf_path(root: Path | None = None, environ: dict[str, str] | None = None) -> Path:
    """`<config>/repo-sync/repos.<profile>.conf`.

    `conf_paths` adds the common conf beside it; this names the profile one.
    `root` names the folder that holds the lists instead of the config dir.

    Resolved from the environment rather than from the working directory:
    the seed runs from cron, whose working directory is the operator's home.
    """
    base = Path(root) if root is not None else config.config_dir(CONF_TOOL, environ)
    return base / f"repos.{profile(environ)}.conf"


def conf_paths(root: Path | None = None, environ: dict[str, str] | None = None) -> list[Path]:
    """The pair `repo-sync` reads: common first, then the profile conf.

    The same order `repo-sync.sh` iterates, so a list reads in conf order.
    Terra reads its profile conf alone, as `repo-sync.sh` does.
    """
    chosen = conf_path(root, environ)
    if profile(environ) == "terra":
        return [chosen]
    return [chosen.with_name(COMMON_CONF_NAME), chosen]


def missing_conf(path: Path) -> ConfMissing:
    """The st_missing-style refusal for an absent profile conf."""
    return ConfMissing(
        f"{path.name} is not set. Copy local-repo-sync/{path.name}.example "
        f"(or another profile's example) to {path} and fill it in, or name a "
        f"conf: `sd-db.sh repo seed <conf>`."
    )


def read_conf(path: Path | str) -> list[tuple[str, str]]:
    """Every `<group> <owner/repo>` line, in file order.

    A line this grammar does not match is a refusal and not a skip. The file
    drives the clone; a line nobody parses is a repository nobody syncs, and
    discovering that from a missing directory months later is the expensive
    way to find out.
    """
    text = Path(path).read_text(encoding="utf-8")
    entries: list[tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = CONF_LINE.match(stripped)
        if match is None:
            raise RepoRefusal(
                f"{path}:{number}: {stripped!r} is not `<subdir> <owner/repo>`; "
                f"the format is the file's own first line"
            )
        entries.append((match.group("group"), match.group("slug")))
    return entries


def checkouts(
    path: Path | str | None = None,
    *,
    root: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> list[Checkout]:
    """The conf resolved against the disk, present flag and all.

    A named `path` is read alone. Without one it is the common and personal
    pair; an entry both name is one checkout, and a missing common file is
    read as empty -- the profile file is the one a seed cannot do without,
    and its absence raises `ConfMissing` naming where to put it.
    """
    base = Path(root) if root is not None else repo_root(environ)
    if path is not None:
        entries = read_conf(path)
    else:
        *common, chosen = conf_paths(environ=environ)
        if not chosen.exists():
            raise missing_conf(chosen)
        entries = [entry for extra in common if extra.is_file() for entry in read_conf(extra)]
        entries = list(dict.fromkeys(entries + read_conf(chosen)))
    found = []
    for group, slug in entries:
        name = slug.split("/", 1)[1]
        target = base / group / name
        found.append(
            Checkout(group=group, slug=slug, path=target, present=(target / ".git").exists())
        )
    return found


def _git(path: Path, *args: str) -> str:
    """Read-only git against a named checkout. Empty on any failure."""
    try:
        done = subprocess.run(  # nosec B603 - fixed argv, shell=False
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def detect(path: Path | str) -> tuple[str | None, str | None]:
    """`(remote, mode)` read from the checkout, never typed by the operator.

    `mode` is `bare` or `work`: item D's runner cares, and a typed answer
    would be wrong the first time somebody converted one. It is the
    checkout's shape, not the pack's `full|minimal|guest` workflow mode;
    that one lives in the repository's `CLAUDE.local.md` and the table does
    not copy it (sd:1666).

    `remote` is stored as the checkout wrote it: it is the transport the
    runner's `ls-remote` and clone use, so an ssh origin stays ssh. Rows
    are compared through `remote_identity`, never by spelling (sd:1666).
    """
    target = paths.expand(path) if str(path).startswith("~") else Path(path)
    remote = _git(target, "remote", "get-url", "origin") or None
    bare = _git(target, "rev-parse", "--is-bare-repository")
    mode = {"true": "bare", "false": "work"}.get(bare)
    return remote, mode


def add(
    connection: sqlite3.Connection,
    path: Path | str,
    *,
    remote: str | None = None,
    mode: str | None = None,
    home: Path | str | None = None,
) -> str:
    """Register one repository. The row is keyed by `paths.key` of the path:
    `~/...` under `$HOME`, the resolved absolute path elsewhere."""
    target = paths.expand(paths.store(path)).resolve()
    if not (target / ".git").exists() and _git(target, "rev-parse", "--git-dir") == "":
        raise RepoRefusal(f"{target} is not a git checkout")
    # Both sides resolved: on macOS a temporary directory reaches this
    # function as `/var/folders/...` and resolves to `/private/var/...`, and
    # an unresolved comparison would let a clone through under exactly the
    # path the refusal exists for.
    worktrees = worktrees_root(home).resolve()
    if worktrees in target.parents or target == worktrees:
        raise RepoRefusal(
            f"{target} is under the worktrees directory {worktrees}; a runner's "
            f"clone is not a registered repository, and registering one would "
            f"give its `docs/work` files rows the sitting cannot keep"
        )
    found_remote, found_mode = detect(target)
    return upsert_repo(
        connection,
        paths.key(str(target)),
        remote=remote or found_remote,
        mode=mode or found_mode,
    )


@dataclass
class Seeded:
    """What a seed run did, so the caller reports it and a rerun matches."""

    registered: list[str]
    absent: list[str]

    @property
    def counts(self) -> dict[str, int]:
        return {"registered": len(self.registered), "absent": len(self.absent)}


def seed(
    connection: sqlite3.Connection,
    path: Path | str | None = None,
    *,
    root: Path | str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | str | None = None,
) -> Seeded:
    """Register every checkout the conf names that this machine actually has.

    Idempotent by construction: `upsert_repo` leaves a row it already wrote
    alone but for the fields it was given, so a second seed registers the same
    set and changes nothing else.
    """
    registered: list[str] = []
    absent: list[str] = []
    for checkout in checkouts(path, root=root, environ=environ):
        if not checkout.present:
            absent.append(checkout.slug)
            continue
        # The checkout's own origin first, the conf's rendering of it second.
        # `Checkout.remote` always renders `https://github.com/<slug>`, because
        # the conf names a slug and not a URL, and passing that as `remote`
        # overrode what `add` had just read off the checkout. A machine that
        # clones over ssh then carried an https remote on every seeded row,
        # and before sd:1436 `same_remote` did not treat the two as one
        # repository -- so `registered_for` stopped resolving a runner clone
        # by origin for exactly the repositories a seed had touched last.
        # The row still records what the checkout says, not a rendering.
        # The conf value stays as the fallback: it is the only answer for a
        # checkout whose origin has been removed.
        found_remote, _ = detect(checkout.path)
        registered.append(
            add(connection, checkout.path,
                remote=found_remote or checkout.remote, home=home)
        )
    return Seeded(registered=sorted(set(registered)), absent=sorted(set(absent)))


#: `scheme://[user[:password]@]host[:port]/path`, the URL form of a remote.
_URL_REMOTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?(?P<host>[^/:]+)(?::\d*)?(?P<path>/.*)?$")

#: `[user@]host:path`, git's scp-like form. A colon before any slash is what
#: separates it from a local path; `C:/...` is excluded by the one-letter host.
_SCP_REMOTE = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^@/:]{2,}):(?P<path>[^/].*)$")


def remote_identity(remote: str | None) -> str:
    """The repository an origin URL names: `github.com/owner/name` on GitHub.

    On github.com the scheme, the ssh user, a port, a trailing slash, a
    `.git` suffix and letter case are spelling, not identity: GitHub serves
    one repository at every one of them. So `git@github.com:o/r.git`,
    `ssh://git@github.com/o/r` and `https://github.com/o/r` all answer
    `github.com/o/r` -- the reduction `local-repo-sync/repo-sync.sh` makes
    before it compares an origin with its conf.

    Any other host is not assumed to work that way. Two ports can be two
    servers, and `alice@host:r` and `bob@host:r` resolve against two home
    directories (sd:1436 review). There only the host's case, a trailing
    slash and a `.git` suffix are removed, and the rest compares as written.
    A local path or `file://` URL, which test fixtures clone from, is the
    same case with no host. An absent remote answers "".
    """
    value = (remote or "").strip()
    if not value:
        return ""
    found = _URL_REMOTE.match(value) or _SCP_REMOTE.match(value)
    if found is not None and found["host"].lower() == "github.com":
        path = (found["path"] or "").strip("/").removesuffix(".git").strip("/")
        return f"github.com/{path.lower()}"
    if found is not None:
        value = value[:found.start("host")] + found["host"].lower() + value[found.end("host"):]
    return value.rstrip("/").removesuffix(".git").rstrip("/")


def same_remote(left: str | None, right: str | None) -> bool:
    """Whether two origin URLs name one repository.

    Both sides reduce through `remote_identity`, so an ssh clone speaks for a
    row that recorded the https spelling (sd:1436). Only spelling is removed:
    a different host, port, user, owner or name is still a different
    repository wherever it can be one, and two absent remotes are never one.
    """
    identity = remote_identity(left)
    return bool(identity) and identity == remote_identity(right)


def registered_for(connection: sqlite3.Connection, root: str, origin: str | None) -> str:
    """The registered repository this checkout *is*, which need not be where it sits.

    R10-D6 says the working directory selects the repository. It does not say
    the working directory *is* the repository's recorded path, and the runner
    is the case that separates the two: it clones from the repository's remote
    into a work path under `/Volumes/sd-work/worktrees/...`, so anything
    resolving by path alone refuses every run the runner makes -- after the
    command has already been queued, dispatched and cloned.

    The path wins when it is itself registered, so an ordinary checkout is
    unaffected and costs one indexed lookup. Otherwise the origin decides, and
    an unrecognised checkout resolves to itself so the caller's own "not
    registered" refusal is what the user sees.
    """
    probe = paths.keys(root)
    found = connection.execute(
        f"SELECT path FROM repo WHERE path IN ({paths.placeholders(probe)}) ORDER BY path",
        probe).fetchone()
    if found:
        return str(found["path"])
    if origin:
        for row in connection.execute(
                "SELECT path, remote FROM repo WHERE remote IS NOT NULL ORDER BY path"):
            if same_remote(row["remote"], origin):
                return str(row["path"])
    return root


def row_for(connection: sqlite3.Connection, path: Path | str) -> sqlite3.Row | None:
    """The `repo` row for a disk path or a key, whichever form the row holds.

    The probe goes through `paths.keys`, so a caller that passes an absolute
    path under the home finds the `~/` row, a caller that passes the key finds
    it too, and a row written before migration 014 still matches (sd:1439).
    """
    probe = paths.keys(path)
    return connection.execute(
        f"SELECT * FROM repo WHERE path IN ({paths.placeholders(probe)}) ORDER BY path",
        probe).fetchone()


#: The columns migration 014 rewrites, each as a query for its stored values
#: and a function from a value to the path it holds (sd:1439).
_KEY_COLUMNS = (
    ("repo.path", "SELECT path FROM repo", lambda value: value),
    ("item.repo", "SELECT repo FROM item WHERE repo IS NOT NULL", lambda value: value),
    ("repo_protection.repo", "SELECT repo FROM repo_protection", lambda value: value),
    ("runner_run.repo", "SELECT repo FROM runner_run", lambda value: value),
    ("runner_lease.repo", "SELECT repo FROM runner_lease", lambda value: value),
    ("item.external_id",
     "SELECT external_id FROM item WHERE source IN ('docs/work', 'writing-piece') "
     "AND instr(external_id, '::') > 0",
     lambda value: value.split("::", 1)[0]),
    ("state.key",
     "SELECT key FROM state WHERE kind = 'verified' "
     "AND (key LIKE '%:status_source' OR key LIKE '%:pieces_source')",
     lambda value: value[:-14]),
    ("cost.repo", "SELECT repo FROM cost WHERE repo IS NOT NULL", lambda value: value),
    ("skill_use.cwd", "SELECT cwd FROM skill_use WHERE cwd IS NOT NULL", lambda value: value),
)


def absolute_under_home(connection: sqlite3.Connection) -> dict[str, int]:
    """How many values in each key column are absolute under this home.

    Migration 014 leaves none, and the library writes none, so a count above
    0 names a writer that bypassed the conversion, or a store not yet at 14.
    A column whose table this store lacks counts 0.
    """
    counts = {}
    for name, query, held in _KEY_COLUMNS:
        try:
            rows = connection.execute(query).fetchall()
        except sqlite3.OperationalError:
            rows = []
        counts[name] = sum(
            1 for (value,) in rows
            if isinstance(value, str) and value.startswith("/")
            and paths.home_relative(held(value)) != held(value))
    return counts


def registered(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every repository the table holds, in path order. The enumeration bound."""
    return list(connection.execute("SELECT * FROM repo ORDER BY path"))


#: What `repo.runner_merge` accepts, in the order the CHECK constraint lists
#: them. Named here so the refusal can print them rather than an IntegrityError.
RUNNER_MERGE_VALUES = ("manual", "auto")


def set_runner_merge(
    connection: sqlite3.Connection,
    path: Path | str,
    value: str,
) -> tuple[str, str]:
    """Set one registered repository's `runner_merge`. Returns `(path, before)`.

    The column had four readers and no caller that set it, so every row kept
    the schema default and the only way to change one was to import the
    library and call `upsert_repo` by hand (sd:1131). This is that caller.

    It refuses an unregistered path rather than creating a row: `add` and
    `seed` are the two ways a row gets into the table and this is not a third.
    The value is checked here so an operator typing `automatic` reads a
    sentence naming the two words, not the CHECK constraint's text.
    """
    if value not in RUNNER_MERGE_VALUES:
        accepted = " or ".join(RUNNER_MERGE_VALUES)
        raise RepoRefusal(f"{value!r} is not a merge setting; expected {accepted}")
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    row = connection.execute(
        f"SELECT path, runner_merge FROM repo WHERE path IN ({paths.placeholders(probe)})",
        probe).fetchone()
    if row is None:
        raise RepoRefusal(
            f"{given} is not a registered repository; run `sd-db.sh repo add {given}`")
    target = row["path"]
    before = row["runner_merge"]
    upsert_repo(connection, target, runner_merge=value)
    return target, before


#: The words `repo managed` takes, mapped to what the column holds. Words and
#: not `1`/`0` so the verb reads like `runner-merge`'s `manual|auto`.
MANAGED_VALUES = {"yes": 1, "no": 0}

def set_managed(
    connection: sqlite3.Connection,
    path: Path | str,
    value: str,
) -> tuple[str, str]:
    """Set one registered repository's `managed` flag. Returns `(path, before)`.

    `before` is the word, `yes` or `no`. Same refusals as `set_runner_merge`:
    a value that is not one of the two words, and a path no row holds.
    """
    if value not in MANAGED_VALUES:
        raise RepoRefusal(f"{value!r} is not a managed setting; expected yes or no")
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    row = connection.execute(
        f"SELECT path, managed FROM repo WHERE path IN ({paths.placeholders(probe)})",
        probe).fetchone()
    if row is None:
        raise RepoRefusal(
            f"{given} is not a registered repository; run `sd-db.sh repo add {given}`")
    target = row["path"]
    before = "yes" if row["managed"] else "no"
    upsert_repo(connection, target, managed=MANAGED_VALUES[value])
    return target, before

