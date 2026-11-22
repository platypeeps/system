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
# check.sh takes the first python3.15 on the caller's PATH.
python3 -m venv "$build"
"$build/bin/python" -m pip install -q --no-index "$pack/lib"
# rename(2) refuses a VENV another gate made meanwhile; that one stands.
if "$build/bin/python" -I -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$build" "$venv" 2>/dev/null; then
  echo "gate venv: built $venv"
else
  [ -x "$venv/bin/python" ] || { echo "gate-venv.sh: could not move $build to $venv" >&2; exit 1; }
  echo "gate venv: reusing $venv, which another gate built"
fi
