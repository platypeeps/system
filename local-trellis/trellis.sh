#!/bin/sh
# TRELLIS.2 image-to-3D on Apple Silicon, through the trellis-mac port.
# Every git checkout is pinned by commit: trellis-mac's setup.sh clones its
# dependencies at their tip, so this script clones each one first, at its
# pin, and setup.sh's `[ ! -d ... ]` guards then skip them. The checkout
# and its venv live on the system disk; the model weights (about 15 GB) go
# to the Hugging Face cache. Background removal uses ZhengPeng7/BiRefNet
# (MIT) in place of the pipeline's briaai/RMBG-2.0 (non-commercial).
# Usage: trellis.sh install|status|generate <image> [--output path]|test|help
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
TRELLIS_HOME="${TRELLIS_HOME:-$HOME/.local/share/trellis-mac}"
STAMP="$TRELLIS_HOME/.local-trellis-installed"
REMBG="TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py"
# The line the swap inserts at the top of BiRefNet.__init__: the pipeline's
# pipeline.json (microsoft/TRELLIS.2-4B) passes model_name briaai/RMBG-2.0,
# and both pipelines build their rembg model through this one class.
SWAP='        model_name = "ZhengPeng7/BiRefNet"  # local-trellis: MIT weights, not briaai/RMBG-2.0'
DINO_URL="https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m"

usage() {
  cat <<'HELPEOF'
usage: trellis.sh install|status|generate <image> [--output path]|test|help

  install    clone trellis-mac and every repository its setup.sh clones,
             each at its pinned commit, into TRELLIS_HOME; run setup.sh
             (venv, Python packages, Metal backends, MPS patches); swap
             background removal to ZhengPeng7/BiRefNet. Safe to rerun: it
             moves a checkout whose pin changed and finishes a stopped run.
             Downloads several GB and builds native code.
  status     ok, or one MISSING/DIFFERS/FAILED line per gap: a checkout off
             its pin, no venv, setup not finished, rembg not swapped, no
             Hugging Face token. Exits 0 when all is in place, 3 when
             nothing is installed, 1 otherwise;
             local-health-check reads these codes.
  generate   generate <image> [--output path] [generate.py options]: run
             trellis-mac's generate.py in the venv; --output is a path
             without extension (default output_3d in the current folder).
  test       run the unittest suite (tests/); extra arguments go to
             unittest (-v).

Two steps stay manual, since this script stores no token:
  hf auth login
  accept the licence at https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m

env:
  TRELLIS_HOME   the checkout (default ~/.local/share/trellis-mac); a
                 path under /Volumes is refused
  TRELLIS_PINS   a file of `<path> <url> <commit>` lines that replaces
                 the pins in this script (tests)
  PYTHON         interpreter for `test` (default python3)
HELPEOF
}

die() { echo "trellis.sh: $*" >&2; exit 1; }

# <path in TRELLIS_HOME> <url> <commit>. Paths and the utils3d commit are
# setup.sh's at trellis-mac d58628f; the others were each repository's
# HEAD on 2026-11-15.
pins() {
  if [ -n "${TRELLIS_PINS:-}" ]; then
    cat "$TRELLIS_PINS"
    return
  fi
  cat <<'PINS'
. https://github.com/shivampkumar/trellis-mac.git d58628f4f5b9c3de8274cb110074154f4b31cef2
deps/utils3d https://github.com/EasternJournalist/utils3d.git 9a4eb15e4021b67b12c460c7057d642626897ec8
deps/mtlbvh https://github.com/pedronaugusto/mtlbvh.git 011896cadb12349d1f4e906af41ff96aa5b5fcb7
deps/mtldiffrast https://github.com/pedronaugusto/mtldiffrast.git db3186be168a495f43ea095f7311a18a4de0a74c
deps/mtlmesh https://github.com/pedronaugusto/mtlmesh.git 4a46faae8ab6def6f308c499e22a23020cdacd5b
deps/mtlgemm https://github.com/pedronaugusto/mtlgemm.git c2e5d3fa15152bc4aef65773cab1ded5a1855d38
deps/trellis2-apple https://github.com/pedronaugusto/trellis2-apple.git c873bde81ebe1e56535c6caddb4b21267364a923
TRELLIS.2 https://github.com/microsoft/TRELLIS.2.git 75fbf0183001ed9876c8dbb35de6b68552ee08bd
PINS
}

# The checkout folder of a pin path.
dest_of() {
  if [ "$1" = . ]; then echo "$TRELLIS_HOME"; else echo "$TRELLIS_HOME/$1"; fi
}

# A checkout's HEAD, or nothing when there is none.
head_of() {
  git -C "$1" rev-parse -q --verify HEAD 2>/dev/null || :
}

refuse_volume() {
  case "$TRELLIS_HOME" in
    /Volumes|/Volumes/*) die "TRELLIS_HOME $TRELLIS_HOME is on a volume; keep it on the system disk" ;;
  esac
  if [ -d "$TRELLIS_HOME" ]; then
    case "$(cd -P "$TRELLIS_HOME" && pwd)" in
      /Volumes|/Volumes/*) die "TRELLIS_HOME $TRELLIS_HOME resolves to a volume; keep it on the system disk" ;;
    esac
  fi
}

# Bring one checkout to its pin: clone it when absent, fetch and check out
# the pin when HEAD differs. --force: setup.sh patches TRELLIS.2 in place,
# and reruns those patches after a move.
checkout_pin() { # path url commit
  cp_dest=$(dest_of "$1")
  if [ ! -e "$cp_dest/.git" ]; then
    if [ -d "$cp_dest" ] && [ "$1" != . ] && [ -n "$(ls -A "$cp_dest")" ]; then
      die "$cp_dest exists and is no git checkout; move it away and rerun install"
    fi
    mkdir -p "$cp_dest"
    git -C "$cp_dest" init -q
  fi
  if git -C "$cp_dest" remote get-url origin >/dev/null 2>&1; then
    [ "$(git -C "$cp_dest" remote get-url origin)" = "$2" ] ||
      git -C "$cp_dest" remote set-url origin "$2"
  else
    git -C "$cp_dest" remote add origin "$2"
  fi
  if [ "$(head_of "$cp_dest")" = "$3" ]; then
    echo "ok      $1 at $3"
    return
  fi
  echo "fetch   $1 at $3"
  git -C "$cp_dest" fetch -q --depth 1 origin "$3"
  git -C "$cp_dest" checkout -q --force --detach FETCH_HEAD
  [ "$(head_of "$cp_dest")" = "$3" ] || die "$1 did not reach $3"
}

# Insert SWAP as the first line of BiRefNet.__init__, once.
swap_rembg() {
  sr_file="$TRELLIS_HOME/$REMBG"
  [ -f "$sr_file" ] || die "$REMBG is missing; the TRELLIS.2 pin no longer has the rembg hook"
  if grep -qxF "$SWAP" "$sr_file"; then
    echo "ok      rembg already ZhengPeng7/BiRefNet"
    return
  fi
  sr_n=$(grep -c '^    def __init__(self, model_name' "$sr_file" || :)
  [ "$sr_n" = 1 ] ||
    die "$REMBG has $sr_n 'def __init__(self, model_name' lines, not 1; the swap needs updating for this pin"
  SWAP="$SWAP" awk '{ print } /^    def __init__\(self, model_name/ { print ENVIRON["SWAP"] }' \
    "$sr_file" > "$sr_file.tmp"
  mv "$sr_file.tmp" "$sr_file"
  echo "swap    rembg briaai/RMBG-2.0 -> ZhengPeng7/BiRefNet"
}

manual_steps() {
  cat <<EOF

Two manual steps, once per machine (no token is stored here):
  1. hf auth login
  2. accept the licence at $DINO_URL
EOF
}

# Whether a run already finished at these pins: the record matches, every
# checkout is at its pin, the venv and the swap are there. Setup then has
# nothing to do, and machine-setup's --apply, which runs install for any
# status gap, does not rebuild the Metal backends for a missing token.
finished() {
  [ -f "$STAMP" ] && pins | cmp -s - "$STAMP" || return 1
  [ -x "$TRELLIS_HOME/.venv/bin/python" ] || return 1
  grep -qxF "$SWAP" "$TRELLIS_HOME/$REMBG" 2>/dev/null || return 1
  pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    [ "$(head_of "$(dest_of "$path")")" = "$sha" ] || return 1
    [ "$(git -C "$(dest_of "$path")" remote get-url origin 2>/dev/null)" = "$url" ] || return 1
  done
}

cmd_install() {
  refuse_volume
  if finished; then
    echo "ok      setup finished at these pins"
    manual_steps
    return
  fi
  # The stamp goes first: a run stopped at any later step reads as
  # unfinished in status until a rerun gets through every step.
  rm -f "$STAMP"
  pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    checkout_pin "$path" "$url" "$sha"
  done
  pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    [ "$(head_of "$(dest_of "$path")")" = "$sha" ] || die "$path is not at $sha"
  done
  (cd "$TRELLIS_HOME" && bash ./setup.sh) || die "setup.sh failed; rerun install"
  swap_rembg
  pins > "$STAMP"
  manual_steps
}

cmd_status() {
  if [ ! -e "$TRELLIS_HOME/.git" ]; then
    echo "MISSING trellis-mac at $TRELLIS_HOME -- run: trellis.sh install"
    exit 3
  fi
  st_bad=0
  st_out=$(pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    st_head=$(head_of "$(dest_of "$path")")
    if [ -z "$st_head" ]; then
      echo "MISSING $path, pinned $sha"
    elif [ "$st_head" != "$sha" ]; then
      echo "DIFFERS $path at $st_head, pinned $sha"
    fi
  done)
  [ -z "$st_out" ] || { printf '%s\n' "$st_out"; st_bad=1; }
  st_py="$TRELLIS_HOME/.venv/bin/python"
  if [ ! -x "$st_py" ]; then
    echo "MISSING venv $TRELLIS_HOME/.venv -- run: trellis.sh install"
    st_bad=1
  fi
  if [ ! -f "$STAMP" ] || ! pins | cmp -s - "$STAMP"; then
    echo "DIFFERS setup not finished at these pins -- run: trellis.sh install"
    st_bad=1
  fi
  if ! grep -qxF "$SWAP" "$TRELLIS_HOME/$REMBG" 2>/dev/null; then
    echo "DIFFERS rembg is not swapped to ZhengPeng7/BiRefNet -- run: trellis.sh install"
    st_bad=1
  fi
  if [ -x "$st_py" ]; then
    # A failed probe proves nothing either way, so it is FAILED, never ok.
    st_tok=$("$st_py" -c 'from huggingface_hub import get_token; print("yes" if get_token() else "no")' \
      </dev/null 2>/dev/null) || st_tok=failed
    case "$st_tok" in
      yes) ;;
      no) echo "DIFFERS no Hugging Face token -- run: hf auth login"; st_bad=1 ;;
      *) echo "FAILED  Hugging Face token probe in the venv"; st_bad=1 ;;
    esac
  fi
  if [ "$st_bad" -ne 0 ]; then
    exit 1
  fi
  echo "ok      trellis-mac $(head_of "$TRELLIS_HOME") at $TRELLIS_HOME"
}

cmd_generate() {
  [ "$#" -ge 1 ] || { usage >&2; exit 1; }
  [ -x "$TRELLIS_HOME/.venv/bin/python" ] ||
    die "no venv at $TRELLIS_HOME/.venv; run: trellis.sh install"
  exec "$TRELLIS_HOME/.venv/bin/python" "$TRELLIS_HOME/generate.py" "$@"
}

case "${1:-}" in
  install) cmd_install ;;
  status) cmd_status ;;
  generate) shift; cmd_generate "$@" ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help) usage; exit 0 ;;
  "") usage >&2; exit 1 ;;
  *) echo "trellis.sh: unknown command '$1'" >&2; usage >&2; exit 1 ;;
esac
