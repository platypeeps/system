#!/bin/sh
# The gate's venv: python3 with the pack's lib/ (sd_db) installed.
# tests/check.sh names VENV by a key of the pack's lib/ tree and the python
# (sd:9926), so a gate reuses the venv a gate before it built; the
# preflight and each macOS-only suite each built their own before.
# It builds beside VENV and renames it in whole, so a gate that stops
# mid-build or two gates at once leave no half-built VENV.
set -e
case "${1:-}" in
  -h|--help|help)
    echo "Usage: gate-venv.sh VENV PACK: build VENV with PACK/lib installed, unless it exists"
    exit 0 ;;
esac
[ "$#" -eq 2 ] || { echo "Usage: gate-venv.sh VENV PACK" >&2; exit 1; }
venv="$1" pack="$2"
if [ -x "$venv/bin/python" ]; then
  touch "$venv"  # the cache prune in tests/check.sh goes by this mtime
  echo "gate venv: reusing $venv"
  exit 0
fi
mkdir -p "$(dirname "$venv")"
build="$venv.build.$$"
rm -rf "$build"
trap 'rm -rf "$build"' EXIT
# No --copies: a copied uv-managed python cannot find its libpython, and
# check.sh takes the first python3.15 on the caller's PATH. The venv is
# built by python3's real path: check.sh links python3 in the gate's own
# worktree, venv points bin/python at the path it ran as, and the cached
# venv dangled once that worktree went (sd:9926 review).
py="$(realpath "$(command -v python3)")"
"$py" -m venv "$build"
"$build/bin/python" -m pip install -q --no-index "$pack/lib"
# Every publisher, a miss or a repair, holds a lock on the cache folder
# and reads VENV again under it: a good VENV another gate put in place
# stands. A VENV whose python does not run (a link that dangles) is
# broken: it is moved aside before the rename and removed after it, and
# an aside a killed publisher left goes too, so a gate never removes a
# VENV in use (sd:9926 review). The lock goes with the process, so a
# killed gate leaves none behind.
publish() {
  "$build/bin/python" -I -c 'import fcntl, glob, os, shutil, sys
build, venv = sys.argv[1:]
good = lambda: os.access(os.path.join(venv, "bin", "python"), os.X_OK)
lock = os.open(os.path.dirname(venv), os.O_RDONLY)
fcntl.flock(lock, fcntl.LOCK_EX)
if good():
    sys.exit(3)
asides = glob.glob(glob.escape(venv) + ".broken.*")
broken = os.path.lexists(venv)
if broken:
    asides.append(f"{venv}.broken.{os.getpid()}")
    os.rename(venv, asides[-1])
try:
    os.rename(build, venv)
finally:
    for aside in asides:
        shutil.rmtree(aside, ignore_errors=True)
sys.exit(2 if broken else 0)' "$build" "$venv"
}
status=0
publish || status=$?
case "$status" in
  0) echo "gate venv: built $venv" ;;
  2) echo "gate venv: built $venv, in place of a broken one" ;;
  3) echo "gate venv: reusing $venv, which another gate built" ;;
  *) echo "gate-venv.sh: could not move $build to $venv" >&2; exit 1 ;;
esac
