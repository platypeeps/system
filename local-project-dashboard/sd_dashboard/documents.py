"""Generated HTML documents, listed here and served as their authors wrote them.

The other screens read rows and render markup. This one does neither: a
generated report is already a finished page, with its own typography and its
own inline SVG, and the useful thing is to hand it over whole rather than to
strip it down into a table.

That decides the two rules this module lives by.

**It serves, it does not embed.** Pulling a report's body into a dashboard page
would run it through the markup filter, which exists to flatten anything a
foreign document brought with it. The filter is right and the report would be
ruined: no stylesheet, no chart. So a report gets its own address and arrives
intact, and the dashboard shows a listing that links to it.

**It knows about no repository in particular.** A report belongs to the
repository that generates it, and a fleet dashboard that had learned the path
to one association's reports would be wrong in a way that is hard to undo
later.

**It finds the roots rather than reciting them.** Any checkout under
`REPO_ROOT` holding `docs/dashboard` is a document root. A hand-kept list of
repositories drifts the moment somebody adds one, and the directory is the
fact the list was trying to describe. `documents.conf` stays for the two
things a directory cannot say: a root that lives somewhere else, a root that
is found but unwanted, and a root whose directory name is not what to call it.
That last line form carries no path on purpose. A rename that repeats the
default location puts the drift back one line lower down.

**A key with two owners has no owner.** Two groups may hold a checkout of the
same name, and the key is the checkout's name. Serving one of them under that
key would serve one repository's pages at another's address, so neither is
served and the listing names the contest. The config settles it.

Serving a file from disk under the dashboard's own origin is the part worth
being careful about, so three things are true at once: the key must name a
configured root exactly, the file name may not contain a separator at all, and
the resolved path's parent must still be that root, which is what stops a
symlink walking out of it.

Finding roots rather than being told them adds a fourth, because it moves the
decision from a person to a glob. A found `docs/dashboard` must resolve inside
the checkout that claims it. Serving a directory outside a checkout is a thing
somebody asks for with a `root|` line, and a symlink is not somebody asking.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .markup import join, tag
from .pages import empty, page

def _config_dir() -> Path:
    """`<config>/project-dashboard`, by the checkout's shared rule."""
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import system_tools_config
    return system_tools_config.config_dir("project-dashboard")


#: In this machine's config directory (`$SYSTEM_TOOLS_CONFIG`, default
#: `~/.config/system`), never supplied by a request. The checkout ships
#: `documents.conf.example` only.
CONFIG = _config_dir() / "documents.conf"

#: Where checkouts live. Same name and same default as the collectors use, so
#: one variable moves the collectors and the `docs/dashboard` scan to another
#: tree. It does not move a `root|` line in `documents.conf`: that line
#: carries its own path, and a move edits it too.
REPO_ROOT = Path(os.environ.get("REPO_ROOT", "~/repos")).expanduser()

#: The directory a repository publishes its finished pages into. Holding one
#: is what makes a checkout a document root; nothing else has to be declared.
PUBLISHED = "docs/dashboard"

#: No separator, no leading dot, and an extension we are willing to serve.
#: Traversal is not defended against here so much as made unsayable.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html")

#: The two inline scripts an archify diagram page carries, by digest. They are
#: byte-identical across every diagram and every diagram type, which is what
#: makes pinning them possible: the alternative -- deriving a hash per file --
#: would let any document served here authorize its own script, which is not a
#: policy at all. A diagram page whose scripts do not match these two simply
#: does not run them; it still renders, because the SVG is in the markup.
#: Re-measure with `dashboard.sh docs --hashes` after upgrading archify.
DIAGRAM_SCRIPTS = (
    "sha256-2bHRpy6NCJnBMLP1uWLpzCNclUMg79W5Fzj8fNLlOAI=",
    "sha256-Hf2euWzsppfIgB74TEEv3Gtu0cBIeeq/DW08HLPSEn4=",
)

#: The document's own policy, replacing the dashboard's for that one response.
#: Strictly tighter where it counts: the dashboard allows same-origin script by
#: origin, and this allows only two exact scripts by digest. It has to permit
#: inline style, because a standalone report carries its stylesheet in its head
#: -- that is what makes it standalone, and what lets it open from a mail
#: client or a Drive preview. `blob:` in img-src is the diagram's PNG export,
#: which builds an object URL for the download; no blob is ever fetched as
#: script, since script-src names digests and nothing else.
POLICY = (
    "default-src 'none'; "
    "script-src " + " ".join(f"'{digest}'" for digest in DIAGRAM_SCRIPTS) + "; "
    "style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
)

#: A document opts in to running its own script by its name, and only by its
#: name (sd:1502). An interactive page -- a map, a chart a reader can drag --
#: needs script, and the policy above refuses it on purpose.
APP_SUFFIX = ".app.html"

#: The one host an opted-in page may load script and stylesheets from. Its
#: tags carry `integrity` hashes, which is the author's half of the bargain;
#: the host is the server's half.
APP_HOST = "https://cdnjs.cloudflare.com"

#: An image host an `img|<key>|<host>` line may name: lowercase, dotted, no
#: scheme, no path. A tile server is the reason a map needs one.
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")

_SCRIPT = re.compile(rb"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_SRC = re.compile(rb"\bsrc\s*=", re.IGNORECASE)


@dataclass(frozen=True)
class Root:
    key: str
    label: str
    path: Path
    #: File names a `skip|<key>|<file>` line withholds: neither listed nor served.
    skip: frozenset[str] = frozenset()
    #: Hosts `img|<key>|<host>` lines name, which this root's opted-in pages may load images from.
    images: tuple[str, ...] = ()


def _label(key: str) -> str:
    """A readable name out of a directory name, which is all a found root has.

    It cannot recover an abbreviation somebody had in mind: `mcp-research`
    becomes `Mcp Research` and not `MCP`. That is what a `root|` line in the
    config is for, and wanting a better label is the one reason to write one.
    """
    words = [w for w in re.split(r"[-_]+", key) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or key


def _base(repo_root: Path | None = None) -> Path:
    """The tree the scan walks, so the scan and the page name the same one."""
    return Path(REPO_ROOT if repo_root is None else repo_root).expanduser()


def _inside(directory: Path, repo: Path) -> bool:
    """Whether a found directory stays within the checkout that names it.

    Both sides resolved, because a symlink is exactly what is being asked
    about and an unresolved compare would answer the wrong question. A
    checkout that is itself a symlink is still its own boundary: somebody put
    the checkout there, and its `docs/dashboard` has not reached past it.
    """
    try:
        return directory.resolve(strict=True).is_relative_to(repo.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False


def _claims(repo_root: Path | None = None) -> dict[str, list[Path]]:
    """Every published directory on disk, gathered under the key it claims.

    Checkouts sit directly under the root or one group deep, which is the
    layout `collect_research` already walks.

    The directory is gitignored in every repository that has one -- the pages
    are built and not tracked -- so nothing here may ask git what exists. A
    directory on disk is the whole test.

    Gathering rather than picking is the point. A key belongs to a basename,
    and two groups may hold the same basename; deciding here which one wins
    would decide it by glob order, which is not a decision.

    A found directory must resolve inside the checkout that claims it. A
    declared root is somebody saying "serve this"; a found one is nobody
    saying anything, so it may not reach past the checkout it was found in.
    Without that, `docs/dashboard -> /outside/private-reports` publishes a
    directory under the dashboard's own origin with no line anywhere granting
    it, and `within()` cannot catch it: `within()` resolves the symlink and
    compares against the root it was handed, which is the right rule for a
    root somebody declared and no rule at all for one nobody did. A directory
    outside stays reachable, through the `root|` line that is the consent this
    path has not got.
    """
    base = _base(repo_root)
    try:
        groups = [base] + [d for d in sorted(base.glob("*")) if d.is_dir()]
    except OSError:
        return {}
    claims: dict[str, list[Path]] = {}
    for group in groups:
        for directory in sorted(group.glob("*/" + PUBLISHED)):
            if not directory.is_dir():
                continue
            # Walk back up PUBLISHED to the checkout, which is what names the
            # root. Off by one and every key is `dashboard`.
            repo = directory
            for _ in Path(PUBLISHED).parts:
                repo = repo.parent
            key = repo.name
            if not NAME.fullmatch(key + ".html"):
                continue
            if not _inside(directory, repo):
                continue
            paths = claims.setdefault(key, [])
            if directory not in paths:
                paths.append(directory)
    return {key: sorted(paths) for key, paths in sorted(claims.items())}


def enumerated(repo_root: Path | None = None) -> list[Root]:
    """The checkouts whose key nobody else claims, in key order."""
    return [Root(key, _label(key), paths[0])
            for key, paths in _claims(repo_root).items() if len(paths) == 1]


def contested(repo_root: Path | None = None) -> dict[str, list[Path]]:
    """Keys more than one checkout claims, with every checkout that claims one.

    Neither is served. Picking one would serve a repository's pages under
    another's address, and the first-wins version of that is worse still: it
    turns on glob order, so a new checkout sorting earlier silently repoints
    an address that already worked. A key with two owners has no owner until
    somebody says which, and `documents.conf` is where they say it.
    """
    return {key: paths for key, paths in _claims(repo_root).items() if len(paths) > 1}


@dataclass(frozen=True)
class Config:
    """The things the file says, none of which is an inventory."""

    roots: list[Root]
    skipped: set[str]
    labels: dict[str, str]
    files: dict[str, set[str]]
    images: dict[str, list[str]]


def _configured(config_path: Path | None = None) -> Config:
    """What the file adds, withholds and renames, in the order it says them."""
    path = config_path or CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Config([], set(), {}, {}, {})
    found: list[Root] = []
    seen: set[str] = set()
    skipped: set[str] = set()
    labels: dict[str, str] = {}
    files: dict[str, set[str]] = {}
    images: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 2 and parts[0] == "skip" and parts[1]:
            skipped.add(parts[1])
            continue
        if len(parts) == 3 and parts[0] == "skip" and parts[1] and parts[2]:
            files.setdefault(parts[1], set()).add(parts[2])
            continue
        if len(parts) == 3 and parts[0] == "label" and parts[1] and parts[2]:
            labels.setdefault(parts[1], parts[2])
            continue
        if len(parts) == 3 and parts[0] == "img" and parts[1] and HOST.fullmatch(parts[2]):
            hosts = images.setdefault(parts[1], [])
            if parts[2] not in hosts:
                hosts.append(parts[2])
            continue
        if len(parts) != 4 or parts[0] != "root":
            continue
        _, key, label, directory = parts
        if not key or key in seen or not NAME.fullmatch(key + ".html"):
            continue
        seen.add(key)
        found.append(Root(key, label or key, Path(directory).expanduser()))
    return Config(found, skipped, labels, files, images)


def roots(config_path: Path | None = None, repo_root: Path | None = None) -> list[Root]:
    """Every document root: what the file names, then what the disk holds.

    A `root|` line wins over a found root of the same key, and is for a root
    that genuinely lives somewhere else, outside any checkout's
    `docs/dashboard`. A `skip|<key>` line drops a found root without hiding
    the directory, and a `skip|<key>|<file>` line drops one file of a root. A
    `label|<key>` line renames one and says nothing about where it is, which
    is the whole of what most of them ever wanted to say.
    """
    config = _configured(config_path)
    found = list(config.roots)
    keys = {root.key for root in found}
    for root in enumerated(repo_root):
        if root.key in keys or root.key in config.skipped:
            continue
        keys.add(root.key)
        found.append(Root(root.key, config.labels.get(root.key) or root.label, root.path))
    return [Root(root.key, root.label, root.path, frozenset(config.files.get(root.key, ())),
                 tuple(config.images.get(root.key, ()))) for root in found]


def unresolved(config_path: Path | None = None,
               repo_root: Path | None = None) -> dict[str, list[Path]]:
    """Contested keys the config has not settled, which the page must name.

    A dropped root that says nothing is the same silence the enumeration was
    written to remove, so the listing reports these rather than omitting them.
    A `root|` line settles a key by claiming it and a `skip|` line by refusing
    it; either way somebody has said which, and there is nothing left to ask.
    """
    config = _configured(config_path)
    # A `label|` line does not settle a contest: it renames a root and says
    # nothing about which directory is the one meant, which is the question.
    claimed = {root.key for root in config.roots} | config.skipped
    return {key: paths for key, paths in contested(repo_root).items()
            if key not in claimed}


def _root(key: str, config_path: Path | None = None,
          repo_root: Path | None = None) -> Root | None:
    for root in roots(config_path, repo_root):
        if root.key == key:
            return root
    return None


def within(directory: Path, name: str) -> Path | None:
    """The one rule, in the one place the listing and the server both call.

    They have to agree. A listing that offers a link the server then refuses
    is worse than a listing that omits the file, because the reader believes
    the first one.
    """
    if not NAME.fullmatch(name or ""):
        return None
    try:
        base = directory.expanduser().resolve(strict=True)
        target = (base / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    # A symlink inside the directory can still point anywhere. Compare the
    # resolved parent, which is what a symlink cannot fake.
    if target.parent != base or not target.is_file():
        return None
    return target


def resolve(key: str, name: str, config_path: Path | None = None,
            repo_root: Path | None = None) -> Path | None:
    """The file this address names, or None if the address does not name one.

    None rather than an exception: every way of missing is the same 404 to the
    reader, and separating "no such root" from "escaped the root" in the
    response would only tell a prober which of the two they achieved.
    """
    found = located(key, name, config_path, repo_root)
    return None if found is None else found[1]


def located(key: str, name: str, config_path: Path | None = None,
            repo_root: Path | None = None) -> tuple[Root, Path] | None:
    """`resolve`, with the root it found: the server reads the root's image hosts."""
    root = _root(key, config_path, repo_root)
    target = None if root is None or name in root.skip else within(root.path, name)
    return None if target is None else (root, target)


def policy(root: Root, name: str, body: bytes) -> str:
    """The policy one served document carries: `POLICY`, or the sandbox for an opted-in page.

    An opted-in page runs in a sandbox without `allow-same-origin`, so its
    origin is opaque. It holds no dashboard cookie and reads no dashboard
    storage, and the server refuses its `Origin: null` on `/api/`. It may
    reach nothing: no `connect-src`, no form, no frame. Its script is the
    host's, by `integrity`, and its own inline scripts, by digest. Digests
    rather than `'unsafe-inline'` because the page renders data: a value that
    smuggles in an `onerror=` attribute must not run.

    The digest is measured here, from the bytes being sent. That is the
    per-file derivation `DIAGRAM_SCRIPTS` refuses, and the opt-in is what
    pays for it: naming a file `*.app.html` is somebody saying its own script
    may run, and the sandbox is what makes saying so cheap.
    """
    if not name.endswith(APP_SUFFIX):
        return POLICY
    digests = sorted({_digest(match[2]) for match in _SCRIPT.finditer(body) if not _SRC.search(match[1])})
    images = ["data:", "blob:", APP_HOST, *(f"https://{host}" for host in root.images)]
    return ("sandbox allow-scripts allow-downloads; default-src 'none'; "
            f"script-src {' '.join([APP_HOST, *digests])}; "
            f"style-src 'unsafe-inline' {APP_HOST}; "
            f"img-src {' '.join(images)}; "
            "font-src data:; connect-src 'none'; form-action 'none'; "
            "frame-ancestors 'none'; base-uri 'none'")


def _digest(script: bytes) -> str:
    """A CSP hash source for one inline script, as the browser measures it.

    The HTML parser turns CRLF and a lone CR into LF before the script sees
    its text, so the digest is taken after the same turn.
    """
    text = script.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(text).digest()).decode("ascii")


def documents(root: Root) -> list[dict]:
    """Every servable file in one root, newest first."""
    found = []
    try:
        entries = list(os.scandir(root.path.expanduser()))
    except OSError:
        return []
    for entry in entries:
        # Checked against the root in hand, never against whatever the live
        # config happens to say: the listing has to describe the directory it
        # was given, or a test passes while the page is wrong.
        if entry.name in root.skip or within(root.path, entry.name) is None:
            continue
        stat = entry.stat()
        found.append({
            "name": entry.name,
            "href": f"/documents/{root.key}/{entry.name}",
            "bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone(),
        })
    found.sort(key=lambda d: d["modified"], reverse=True)
    return found


def _age(when: datetime, now: datetime) -> str:
    days = (now.date() - when.date()).days
    if days <= 0:
        return "today"
    return "yesterday" if days == 1 else "%d days ago" % days


def render(parameters=None, *, config_path: Path | None = None,
           repo_root: Path | None = None, now=None) -> str:
    """The listing. One section per root, each root's files newest first."""
    del parameters  # The listing takes no facets; every root is always shown.
    moment = now or datetime.now().astimezone()
    configured = roots(config_path, repo_root)
    contests = unresolved(config_path, repo_root)
    if not configured and not contests:
        return page("Documents", "documents",
                    empty("No document roots are configured.",
                          "publish into %s in a checkout under %s"
                          % (PUBLISHED, _base(repo_root))))

    sections: list[object] = []
    for root in configured:
        found = documents(root)
        if not found:
            sections.append(tag(
                "section",
                tag("h2", root.label),
                tag("p", "Nothing generated yet in %s." % root.path, class_="notice"),
            ))
            continue
        rows = [
            tag("li", join([
                tag("a", document["name"], href=document["href"]),
                tag("span", " %s, %.0f KB" % (_age(document["modified"], moment),
                                              document["bytes"] / 1024.0),
                    class_="hint"),
            ]))
            for document in found
        ]
        sections.append(tag(
            "section",
            tag("h2", root.label),
            tag("p", str(root.path), class_="hint"),
            tag("ul", join(rows), class_="documents"),
        ))

    for key, paths in contests.items():
        sections.append(tag(
            "section",
            tag("h2", _label(key)),
            tag("p", "%d checkouts claim the key %s, so none of them is served. "
                     "Name the one you mean with a root| line in "
                     "%s." % (len(paths), key, CONFIG),
                class_="notice"),
            tag("ul", join([tag("li", str(path)) for path in paths]),
                class_="documents"),
        ))

    return page(
        "Documents", "documents",
        tag("p", "Reports their own repositories generate. This page lists them and "
                 "serves them unchanged; it never edits one, and a republish is the "
                 "generating repository's business.", class_="hint"),
        join(sections),
    )
