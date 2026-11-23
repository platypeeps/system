"""Back up sensitive files to the 1Password vault Backups (sd:10098).

`plan` lists what a run would store. `run` packs the files the targets file
names into a gzip tarball in a private temp folder, stores it with
`op document create`, deletes the tarball, then keeps the newest
KEEP months of this host's backups and deletes older ones.

The targets file is `<config>/sensitive-backup/targets`: one glob per line,
relative to $HOME; `!<pattern>` drops files whose name matches. It is
private config, since the list itself names the operator's tools.

A rerun in one month replaces that month's item rather than adding one: the
new item is created first, then every older item of the same title goes. A
run stopped between the two leaves two, and the next run removes the extra.

Nothing prints a file's contents or a token. op signs in only through
OP_SERVICE_ACCOUNT_TOKEN: without it op asks for Touch ID, and an unattended
run would wait on a prompt nobody sees.
"""

import fnmatch
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

TOOL = "sensitive-backup"
VAULT = "Backups"
TAGS = "backup,sensitive"
KEEP = 12
CREATE_SECS = 600
OP_SECS = 120


class Failure(Exception):
    pass


def host():
    return socket.gethostname().split(".")[0].lower()


def targets_file(environ):
    return system_tools_config.config_dir(TOOL, environ) / "targets"


def read_targets(path):
    """(globs, excludes) from the targets file; refuses unsafe lines."""
    if not path.is_file():
        raise Failure(f"no targets file: copy local-sensitive-backup/targets.example "
                      f"to {path} and list the files to back up")
    globs, excludes = [], []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            if not line[1:] or "/" in line[1:]:
                raise Failure(f"{path}:{number}: an exclude is a bare file-name pattern")
            excludes.append(line[1:])
            continue
        parts = PurePosixPath(line).parts
        if any(c in part for part in parts[:-1] for c in "*?["):
            raise Failure(f"{path}:{number}: a wildcard may stand only in the last "
                          f"part: {line!r}")
        if line.startswith(("/", "~")) or ".." in parts:
            raise Failure(f"{path}:{number}: a target is relative to $HOME, "
                          f"with no '..': {line!r}")
        globs.append(line)
    if not globs:
        raise Failure(f"{path} names no target")
    return globs, excludes


def collect(home, globs, excludes):
    """Regular files under home, by path relative to it.

    Every target must match: a line that matches nothing, or a folder that
    cannot be read, fails the run, since a smaller archive would replace
    the month's complete one. A symlinked folder on the way to a match, or
    as the match itself, fails too: it would pull in a tree outside home.
    Each line must add at least one file, and a file it names directly must
    be a regular file. A symlinked file is stored as its contents; inside a
    target folder, a symlinked folder is not entered, and a broken link or a
    socket is skipped, since it holds no contents to restore.
    """
    def wanted(path):
        return (not any(fnmatch.fnmatchcase(path.name, x) for x in excludes)
                and path.is_file())

    def unreadable(error):
        raise Failure(f"cannot read {error.filename}: {error.strerror}")

    def matching(pattern):
        # The folder is listed with os.listdir, not Path.glob: glob skips a
        # folder it cannot read and reports no error.
        parts = PurePosixPath(pattern).parts
        parent = home.joinpath(*parts[:-1])
        if not any(c in parts[-1] for c in "*?["):
            target = parent / parts[-1]
            return [target] if os.path.lexists(target) else []
        if not parent.is_dir():
            return []
        try:
            names = os.listdir(parent)
        except OSError as error:
            unreadable(error)
        return [parent / n for n in sorted(names) if fnmatch.fnmatchcase(n, parts[-1])]

    files = {}
    for pattern in globs:
        found = {}
        for match in matching(pattern):
            parts = match.relative_to(home).parts
            for depth in range(1, len(parts) + 1):
                step = home.joinpath(*parts[:depth])
                if step.is_symlink() and (depth < len(parts) or step.is_dir()):
                    raise Failure(f"{step.relative_to(home)} is a symlink to a folder; "
                                  f"name the real folder instead")
            if match.is_dir():
                for top, dirs, names in os.walk(match, onerror=unreadable):
                    dirs.sort()
                    for name in sorted(names):
                        path = Path(top) / name
                        if wanted(path):
                            found[path.relative_to(home).as_posix()] = path
            elif any(fnmatch.fnmatchcase(match.name, x) for x in excludes):
                continue
            elif not match.is_file():
                raise Failure(f"{match.relative_to(home)} is not a file "
                              f"(a broken link or a special file)")
            else:
                found[match.relative_to(home).as_posix()] = match
        # Each line must add a file: an emptied folder, or one whose files
        # are all excluded, would shrink the month's backup unseen.
        if not found:
            raise Failure(f"no file matches {pattern}; refusing a partial backup")
        files.update(found)
    return files


def pack(files, folder):
    archive = Path(folder) / "sensitive-files.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT, dereference=True) as tar:
        for name, path in sorted(files.items()):
            try:
                tar.add(path, arcname=name, recursive=False)
            except OSError as error:
                raise Failure(f"cannot read {name}: {error.strerror}") from None
    return archive


def op(args, timeout, parse=True):
    command = [os.environ.get("SENSITIVE_BACKUP_OP", "op"), *args]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failure(f"op {args[0]} {args[1]} did not answer in {timeout} s") from None
    except OSError as error:
        raise Failure(f"op cannot run: {error}") from None
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        raise Failure(f"op {args[0]} {args[1]} exited {done.returncode}: {last}")
    if not parse:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise Failure(f"op {args[0]} {args[1]} printed no JSON") from None


def prune(title_re, new_id, new_title):
    """Delete all but the newest item per month, then all but KEEP months."""
    listed = op(["item", "list", "--vault", VAULT, "--tags", "sensitive",
                 "--format", "json"], OP_SECS)
    if not isinstance(listed, list):
        raise Failure("op item list did not return a list")
    by_title = {new_title: [(1, "", new_id)]}
    for item in listed:
        if not isinstance(item, dict):
            continue
        ident, title = item.get("id"), item.get("title")
        if (not isinstance(ident, str) or not ident or not isinstance(title, str)
                or not title_re.fullmatch(title) or ident == new_id
                or item.get("category") not in (None, "DOCUMENT")):
            continue
        created = item.get("created_at") if isinstance(item.get("created_at"), str) else ""
        by_title.setdefault(title, []).append((0, created, ident))
    doomed = []
    # The month in the title orders the backups, not created_at. This run's
    # title is always kept, first, so later-dated titles (a clock that was
    # once ahead) can never push the document just stored out of the window.
    order = [new_title] + sorted((t for t in by_title if t != new_title), reverse=True)
    for rank, title in enumerate(order):
        entries = sorted(by_title[title], reverse=True)
        doomed += [ident for _, _, ident in (entries if rank >= KEEP else entries[1:])]
    if new_id in doomed:
        raise Failure("retention chose the new document; deleted nothing")
    for ident in doomed:
        op(["document", "delete", ident, "--vault", VAULT], OP_SECS, parse=False)
    return min(len(by_title), KEEP), len(doomed)


def run(mode, environ):
    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    globs, excludes = read_targets(targets_file(environ))
    files = collect(home, globs, excludes)
    if not files:
        raise Failure("the targets match no file; refusing to store an empty backup")
    if mode == "plan":
        for name in sorted(files):
            print(name)
        print(f"plan: {len(files)} files")
        return
    if not environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        raise Failure("OP_SERVICE_ACCOUNT_TOKEN is empty: the job sets it from "
                      "OP_BACKUP_SERVICE_ACCOUNT_TOKEN in ~/.config/shell/env.sh")
    name = host()
    month = time.strftime("%Y-%m")
    title = f"{name} sensitive files {month}"
    folder = tempfile.mkdtemp(prefix="sensitive-backup.")
    try:
        archive = pack(files, folder)
        size = archive.stat().st_size
        created = op(["document", "create", str(archive), "--vault", VAULT,
                      "--title", title, "--tags", TAGS,
                      "--file-name", f"{name}-sensitive-files-{month}.tar.gz",
                      "--format", "json"], CREATE_SECS)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    new_id = (created.get("uuid") or created.get("id")) if isinstance(created, dict) else None
    if not isinstance(new_id, str) or not new_id:
        raise Failure("op document create returned no item id")
    print(f"stored {new_id}: {len(files)} files, {size} bytes, as {title!r}")
    title_re = re.compile(re.escape(name) + r" sensitive files \d{4}-\d{2}")
    try:
        kept, deleted = prune(title_re, new_id, title)
    except Failure as error:
        print(f"SD_REPORT_ATTENTION: backup stored, prune failed: {error}")
        raise
    print(f"kept {kept} months, deleted {deleted} items")


def main(argv):
    mode = argv[1] if len(argv) == 2 else ""
    if mode not in ("run", "plan"):
        print("usage: sensitive_backup.py run|plan", file=sys.stderr)
        return 1
    os.umask(0o077)
    # The runner stops a job with SIGTERM; this turns it into an exception,
    # so the finally above removes the tarball.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        run(mode, os.environ)
    except Failure as error:
        print(f"sensitive-backup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
