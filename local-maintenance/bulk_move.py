"""Move a file or folder to the bulk storage root, then link it (sd:8797).

maintenance.sh runs `python3 bulk_move.py ROOT PATH [FOLDER]`. The copy
goes to ROOT/FOLDER/<path inside its checkout>; FOLDER defaults to the
checkout's name, and outside a checkout it is required (the copy then goes
to ROOT/FOLDER/<basename>). Each file of the copy is checked by size and
content, and for every extended attribute (on macOS a resource fork and
Finder tags are ones); a mismatch removes the copy and keeps the source. Only then the
source is replaced by a symlink to the copy.

Refused, with the reason on stderr and nothing changed: a missing path, a
symlink, a path git tracks or a checkout, a folder holding
a relative link that leads out of it, build output (target/,
node_modules/), a path a process holds open (lsof; an lsof that fails
refuses too), a missing or full bulk root, an existing destination,
a destination outside the bulk root or inside the source, and a copy
with any path under ROOT/*/lane/, which repo-sync's lane sweep empties.

Exit 0: moved. 1: refused or failed; the source is unchanged.
"""

import ctypes
import filecmp
import os
import shutil
import subprocess
import sys

BUILD_OUTPUT = ("target", "node_modules")
HERE = os.path.dirname(os.path.abspath(__file__))


class Refused(Exception):
    pass


def free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def held_open(path):
    """True when lsof lists a name at or under `path`; Refused if lsof fails."""
    sys.path.insert(0, HERE)
    from in_use import in_use
    try:
        run = subprocess.run(["lsof", "-nP", "-F", "n"], check=False, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f"lsof gave no answer ({error}); kept {path}") from None
    # A nonzero exit may come with a partial listing, which proves nothing.
    if run.returncode != 0 or not run.stdout:
        raise Refused(f"lsof gave no full answer (exit {run.returncode}); kept {path}")
    return in_use(path, run.stdout.splitlines(keepends=True))


def walk(src):
    """Every entry under `src` as a path relative to it, links not followed."""
    if not os.path.isdir(src) or os.path.islink(src):
        return []
    found = []
    for top, dirs, files in os.walk(src):
        for name in dirs + files:
            found.append(os.path.relpath(os.path.join(top, name), src))
    return found


def checkout(path):
    """The top of the git checkout holding `path`, or None."""
    where = path if os.path.isdir(path) else os.path.dirname(path)
    run = subprocess.run(["git", "-C", where, "rev-parse", "--show-toplevel"],
                         check=False, capture_output=True, text=True)
    return os.path.realpath(run.stdout.strip()) if run.returncode == 0 else None


def xattrs(path):
    """Extended attributes of `path`, links not followed: name -> bytes.

    On macOS a resource fork and Finder tags are such attributes, and
    Python's os has no xattr calls there, so libc is called directly.
    """
    if hasattr(os, "listxattr"):
        return {n: os.getxattr(path, n, follow_symlinks=False)
                for n in os.listxattr(path, follow_symlinks=False)}
    libc = ctypes.CDLL(None, use_errno=True)
    libc.listxattr.restype = libc.getxattr.restype = ctypes.c_ssize_t
    libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    nofollow = 1  # XATTR_NOFOLLOW
    raw = os.fsencode(path)

    def call(fn, *args):
        size = fn(*args, None, 0, *([0] if fn is libc.getxattr else []), nofollow)
        if size < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), path)
        buf = ctypes.create_string_buffer(size)
        size = fn(*args, buf, size, *([0] if fn is libc.getxattr else []), nofollow)
        if size < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), path)
        return buf.raw[:size]

    names = [n for n in call(libc.listxattr, raw).split(b"\0") if n]
    return {n.decode(): call(libc.getxattr, raw, n) for n in names}


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if sys.platform == "darwin":
        # shutil's copy2 drops resource forks and extended attributes on
        # macOS; Apple's ditto keeps them, with modes and links.
        run = subprocess.run(["/usr/bin/ditto", src, dst], check=False, capture_output=True, text=True)
        if run.returncode != 0:
            raise OSError(f"ditto exited {run.returncode}: {run.stderr.strip()}")
    elif os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def mismatches(src, dst):
    """Entries whose copy differs: kind, link target, size, content, or
    an extended attribute of the source the copy lacks or holds changed."""
    bad = []
    pairs = [(src, dst, os.path.basename(src))]
    pairs += [(os.path.join(src, r), os.path.join(dst, r), r) for r in walk(src)]
    for a, b, rel in pairs:
        if os.path.islink(a):
            if not os.path.islink(b) or os.readlink(a) != os.readlink(b):
                bad.append(rel)
            continue
        if os.path.isdir(a):
            if os.path.islink(b) or not os.path.isdir(b):
                bad.append(rel)
                continue
        elif (os.path.islink(b) or not os.path.isfile(b)
              or os.path.getsize(a) != os.path.getsize(b)
              or not filecmp.cmp(a, b, shallow=False)):
            bad.append(rel)
            continue
        if any(xattrs(b).get(n) != v for n, v in xattrs(a).items()):
            bad.append(f"{rel} (extended attributes)")
    if os.path.isdir(src) and len(walk(dst)) != len(walk(src)):
        bad.append("(entry count)")
    return bad


def remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def plan(root, path, folder):
    """Check every refusal; return (src, dst)."""
    src = os.path.abspath(path)
    if os.path.islink(src):
        raise Refused(f"{src} is a symlink already; nothing to move")
    if not os.path.exists(src):
        raise Refused(f"{src} does not exist")
    src = os.path.join(os.path.realpath(os.path.dirname(src)), os.path.basename(src))
    root = os.path.realpath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise Refused(f"bulk root {root} is missing (unmounted?); kept {src}")
    if src == root or src.startswith(root + os.sep):
        raise Refused(f"{src} is on the bulk root already")
    entries = walk(src)
    parts = src.split(os.sep) + [p for e in entries for p in e.split(os.sep)]
    if any(p in BUILD_OUTPUT for p in parts):
        raise Refused(f"{src} is or holds build output (target/, node_modules/); it stays on the system disk")
    if ".git" in parts:
        raise Refused(f"{src} is or holds a git checkout")
    # A relative link that leaves the folder would name another target
    # once the folder sits on the bulk root.
    for e in entries:
        link = os.path.join(src, e)
        if os.path.islink(link) and not os.path.isabs(os.readlink(link)):
            target = os.path.normpath(os.path.join(os.path.dirname(link), os.readlink(link)))
            if not target.startswith(src + os.sep):
                raise Refused(f"{link} is a relative link out of {src}; it would break on the bulk root")
    top = checkout(src)
    if top:
        rel = os.path.relpath(src, top)
        tracked = subprocess.run(["git", "-C", top, "ls-files", "-z", "--", rel],
                                 capture_output=True, check=True).stdout
        if tracked:
            raise Refused(f"{src} is tracked by git, or holds a tracked file")
        folder = folder or os.path.basename(top)
    else:
        if not folder:
            raise Refused(f"{src} is in no checkout; name the folder under the bulk root")
        rel = os.path.basename(src)
    # realpath folds `..`, an absolute folder and links on the way, so the
    # copy lands beneath the root and never inside what it copies.
    dst = os.path.realpath(os.path.join(root, folder, rel))
    if not dst.startswith(root + os.sep):
        raise Refused(f"{dst} is not beneath the bulk root {root}; kept {src}")
    # repo-sync's hyg_lane deletes files 14 days old under <root>/*/lane/,
    # and the copy keeps the source's mtimes. The literal name matches
    # any case on a case-insensitive volume. Every path of the copy is
    # checked: with folder `.`, a lane/ child of the source lands there.
    for path in [dst] + [os.path.join(dst, e) for e in entries]:
        under = os.path.relpath(path, root).split(os.sep)
        if len(under) > 1 and under[1].lower() == "lane":
            raise Refused(f"{path} would be under {os.path.join(root, under[0], under[1])}, "
                          f"which the lane sweep empties; kept {src}")
    if dst == src or dst.startswith(src + os.sep):
        raise Refused(f"{dst} is inside {src}; kept it")
    if os.path.lexists(dst):
        raise Refused(f"{dst} exists already; kept {src}")
    size = sum(os.lstat(os.path.join(src, e)).st_size for e in entries) or os.lstat(src).st_size
    if free_bytes(root) <= size:
        raise Refused(f"bulk root {root} is full: {size} bytes do not fit; kept {src}")
    if held_open(src):
        raise Refused(f"a process holds {src} open (lsof); kept it")
    return src, dst


def main(argv):
    if len(argv) not in (2, 3):
        print("usage: bulk_move.py ROOT PATH [FOLDER]", file=sys.stderr)
        return 1
    try:
        src, dst = plan(argv[0], argv[1], argv[2] if len(argv) == 3 else None)
    except Refused as why:
        print(f"move: {why}", file=sys.stderr)
        return 1
    try:
        copy(src, dst)
        bad = mismatches(src, dst)
    except OSError as error:
        bad = [f"copy failed: {error}"]
    if bad:
        if os.path.lexists(dst):
            remove(dst)
        print(f"move: the copy of {src} differs ({', '.join(bad[:5])}); removed it, kept the source",
              file=sys.stderr)
        return 1
    aside = f"{src}.bulk-move-{os.getpid()}"
    os.rename(src, aside)
    # A writer may have opened the source during the copy: check again under
    # the new name, which no new open by the old path reaches.
    try:
        late = "a process holds it open (lsof)" if held_open(aside) else ", ".join(mismatches(aside, dst)[:5])
    except (Refused, OSError) as why:
        late = str(why)
    if late:
        os.rename(aside, src)
        remove(dst)
        print(f"move: {src} changed during the copy ({late}); removed the copy, kept the source", file=sys.stderr)
        return 1
    try:
        os.symlink(dst, src)
    except OSError as error:
        os.rename(aside, src)
        print(f"move: could not link {src} ({error}); kept it, the copy stays at {dst}", file=sys.stderr)
        return 1
    remove(aside)
    print(f"moved {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
