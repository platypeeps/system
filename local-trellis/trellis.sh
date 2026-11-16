#!/bin/sh
# TRELLIS.2 image-to-3D on Apple Silicon, through the trellis-mac port.
# Every git checkout is pinned by commit: trellis-mac's setup.sh clones its
# dependencies at their tip, so this script clones each one first, at its
# pin, and setup.sh's `[ ! -d ... ]` guards then skip them. The checkout
# and its venv live on the system disk; the model weights (about 15 GB) go
# to the Hugging Face cache under the bulk storage root, an ejectable SSD.
# Background removal uses ZhengPeng7/BiRefNet (MIT) in place of the
# pipeline's briaai/RMBG-2.0 (non-commercial).
# Usage: trellis.sh install|status|generate <image> [--output path]|test|help
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
TRELLIS_HOME="${TRELLIS_HOME:-$HOME/.local/share/trellis-mac}"
STAMP="$TRELLIS_HOME/.local-trellis-installed"
# What the last finished install left in each checkout: `<path> <HEAD>
# <hash of git diff HEAD>`. Its own patches (C++20, the rembg guard and
# cast, setup.sh's MPS patches) are those diffs; any other diff is an edit
# install did not make, and no pin moves over it.
PATCHES="$TRELLIS_HOME/.local-trellis-patches"
REMBG="TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py"
# The guard the swap inserts at the top of BiRefNet.__init__: the pipeline's
# pipeline.json (microsoft/TRELLIS.2-4B) passes model_name briaai/RMBG-2.0,
# and both pipelines build their rembg model through this one class.
SWAP_IF='        if model_name == "briaai/RMBG-2.0":  # local-trellis: MIT weights'
SWAP_SET='            model_name = "ZhengPeng7/BiRefNet"'
# ZhengPeng7/BiRefNet ships fp16 weights and the input is fp32, so the
# first conv fails ("Input type (float) and bias type (c10::Half)"): the
# model is cast to float right before its eval().
CAST='        self.model.float()  # local-trellis: ZhengPeng7 ships fp16 weights'
DINO_REPO="facebook/dinov3-vitl16-pretrain-lvd1689m"
DINO_URL="https://huggingface.co/$DINO_REPO"
# torch 2.15, which setup.sh installs, needs C++20; these pass -std=c++17.
CXX_FILES="deps/mtlbvh/setup.py deps/mtldiffrast/setup.py deps/mtlmesh/setup.py deps/mtlgemm/setup.py deps/trellis2-apple/o-voxel/setup.py"
# o-voxel builds against this repository's eigen submodule.
SUBMODULE_REPO="deps/trellis2-apple"
TRELLIS_ENV_FILE="${TRELLIS_ENV_FILE:-$HOME/.config/shell/env.sh}"
SD_BIN="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-workflow}/bin/sd"

usage() {
  cat <<'HELPEOF'
usage: trellis.sh install|status|generate <image> [--output path]|test|help

  install    clone trellis-mac and every repository its setup.sh clones,
             each at its pinned commit, into TRELLIS_HOME; run setup.sh
             (venv, Python packages, Metal backends built as C++20 with
             o-voxel's eigen submodule, MPS patches); add einops, which
             BiRefNet imports; swap
             background removal to ZhengPeng7/BiRefNet. Safe to rerun: it
             moves a checkout whose pin changed and finishes a stopped run.
             Downloads several GB and builds native code.
  status     ok, or one MISSING/DIFFERS/FAILED line per gap: a checkout off
             its pin, no venv, no einops, setup not finished, rembg not
             swapped, no Hugging Face token, the DINOv3 licence not
             accepted, the model cache disk not mounted. Exits 0 when all is in place, 3 when
             nothing is installed, 1 otherwise;
             local-health-check reads these codes.
  generate   generate <image> [--output path] [generate.py options]: run
             trellis-mac's generate.py in the venv; --output is a path
             without extension (default output_3d in the current folder).
  test       run the unittest suite (tests/); extra arguments go to
             unittest (-v).

Two steps stay manual, since this script stores no token:
  hf auth login (or export HUGGING_FACE_API_KEY)
  accept the licence at https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m

env:
  TRELLIS_HOME   the checkout (default ~/.local/share/trellis-mac); a
                 path under /Volumes is refused
  TRELLIS_PINS   a file of `<path> <url> <commit>` lines that replaces
                 the pins in this script (tests)
  HF_HUB_CACHE   model cache; when unset, <sd.bulk_storage_root>/system/
                 huggingface/hub, or Hugging Face's default without sd. A
                 cache on an unmounted /Volumes disk stops generate.
  HF_TOKEN       when empty, taken at run time from HUGGING_FACE_API_KEY,
                 exported or in TRELLIS_ENV_FILE (default
                 ~/.config/shell/env.sh); never written anywhere
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

# A checkout's HEAD and the hash of its tracked changes.
fingerprint() {
  printf '%s %s\n' "$(head_of "$1")" \
    "$(git -C "$1" diff --no-ext-diff --binary HEAD -- | git hash-object --stdin)"
}

# The tracked files a checkout changed, one per line.
changed_files() {
  git -C "$1" status --porcelain --untracked-files=no | cut -c4-
}

# Before any pin moves: each checkout whose pin moves must hold no tracked
# change, or exactly what the last finished install left (PATCHES).
# Anything else refuses the whole install, naming the files, and no pin
# moves. A failed read refuses too: it proves nothing.
refuse_edits() {
  re_bad=$(pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    re_dest=$(dest_of "$path")
    re_head=$(head_of "$re_dest")
    [ -n "$re_head" ] && [ "$re_head" != "$sha" ] || continue
    if ! re_files=$(changed_files "$re_dest"); then
      echo "$path: git status failed"
      continue
    fi
    [ -n "$re_files" ] || continue
    re_was=$(awk -v p="$path" '$1 == p { print $2, $3 }' "$PATCHES" 2>/dev/null || :)
    [ -n "$re_was" ] && [ "$(fingerprint "$re_dest")" = "$re_was" ] && continue
    printf '%s\n' "$re_files" | sed "s|^|$path: |"
  done)
  [ -z "$re_bad" ] && return 0
  printf '%s\n' "$re_bad" | sed 's/^/trellis.sh: edit install did not make: /' >&2
  die "no pin moved; commit, stash or revert those edits, then rerun install"
}

# Record what this finished install left in each checkout.
record_patches() {
  pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    printf '%s %s\n' "$path" "$(fingerprint "$(dest_of "$path")")"
  done > "$PATCHES.tmp"
  mv "$PATCHES.tmp" "$PATCHES"
}

# Bring one checkout to its pin: clone it when absent, fetch and check out
# the pin when HEAD differs. refuse_edits has passed, so a tracked change
# here is the installer's own patch: reset drops it, and install applies
# it again. The checkout runs without --force, so git refuses to replace
# an untracked file in the way.
checkout_pin() { # path url commit
  cp_dest=$(dest_of "$1")
  if [ ! -e "$cp_dest/.git" ]; then
    if [ -d "$cp_dest" ] && [ "$1" != . ] && [ -n "$(ls -A "$cp_dest")" ]; then
      die "$cp_dest exists and is no git checkout; move it away and rerun install"
    fi
    mkdir -p "$cp_dest"
    git -C "$cp_dest" init -q
  fi
  if cp_was=$(git -C "$cp_dest" remote get-url origin 2>/dev/null); then
    if [ "$cp_was" != "$2" ]; then
      # Named, so an origin set by hand can be set back.
      echo "origin  $1 was $cp_was, now $2"
      git -C "$cp_dest" remote set-url origin "$2"
    fi
  else
    git -C "$cp_dest" remote add origin "$2"
  fi
  if [ "$(head_of "$cp_dest")" = "$3" ]; then
    echo "ok      $1 at $3"
    return
  fi
  echo "fetch   $1 at $3"
  git -C "$cp_dest" fetch -q --depth 1 origin "$3"
  [ -z "$(head_of "$cp_dest")" ] || git -C "$cp_dest" reset -q --hard HEAD
  git -C "$cp_dest" checkout -q --detach FETCH_HEAD ||
    die "$1: git would not check out $3 (an untracked file in the way?); nothing was removed"
  [ "$(head_of "$cp_dest")" = "$3" ] || die "$1 did not reach $3"
}

# Whether BiRefNet.py maps briaai/RMBG-2.0 to ZhengPeng7/BiRefNet: an `if`
# on that name directly above SWAP_SET, whatever its comment, so a guard
# applied by hand counts too.
guarded() {
  SWAP_SET="$SWAP_SET" awk '
    prev ~ /^        if model_name == "briaai\/RMBG-2.0":/ && $0 == ENVIRON["SWAP_SET"] { found = 1 }
    { prev = $0 }
    END { exit !found }' "$TRELLIS_HOME/$REMBG"
}

# Whether self.model.float() sits directly above self.model.eval(),
# whatever its comment.
cast() {
  awk '
    prev ~ /^        self\.model\.float\(\)( |$)/ && $0 == "        self.model.eval()" { found = 1 }
    { prev = $0 }
    END { exit !found }' "$TRELLIS_HOME/$REMBG"
}

# The swap is done only with both: the name guard and the float cast.
swapped() {
  [ -f "$TRELLIS_HOME/$REMBG" ] && guarded && cast
}

# Insert the guard as the first lines of BiRefNet.__init__ and the cast
# above its eval(), each once. A hook missing at this pin fails install.
swap_rembg() {
  sr_file="$TRELLIS_HOME/$REMBG"
  [ -f "$sr_file" ] || die "$REMBG is missing; the TRELLIS.2 pin no longer has the rembg hook"
  if swapped; then
    echo "ok      rembg already ZhengPeng7/BiRefNet"
    return
  fi
  if ! guarded; then
    sr_n=$(grep -c '^    def __init__(self, model_name' "$sr_file" || :)
    [ "$sr_n" = 1 ] ||
      die "$REMBG has $sr_n 'def __init__(self, model_name' lines, not 1; the swap needs updating for this pin"
    SWAP_IF="$SWAP_IF" SWAP_SET="$SWAP_SET" awk '
      { print }
      /^    def __init__\(self, model_name/ { print ENVIRON["SWAP_IF"]; print ENVIRON["SWAP_SET"] }' \
      "$sr_file" > "$sr_file.tmp"
    mv "$sr_file.tmp" "$sr_file"
    echo "swap    rembg briaai/RMBG-2.0 -> ZhengPeng7/BiRefNet"
  fi
  if ! cast; then
    sr_n=$(grep -c '^        self\.model\.eval()$' "$sr_file" || :)
    [ "$sr_n" = 1 ] ||
      die "$REMBG has $sr_n 'self.model.eval()' lines, not 1; the fp16 cast needs updating for this pin"
    CAST="$CAST" awk '
      $0 == "        self.model.eval()" { print ENVIRON["CAST"] }
      { print }' "$sr_file" > "$sr_file.tmp"
    mv "$sr_file.tmp" "$sr_file"
    echo "cast    rembg model to float before eval"
  fi
}

# Before setup.sh: C++20 for the Metal builds, and o-voxel's eigen. Both
# are idempotent; a checkout that moved gets them again.
prep_build() {
  for pb_f in $CXX_FILES; do
    [ -f "$TRELLIS_HOME/$pb_f" ] || die "$pb_f is missing; the pins need review"
    sed 's/-std=c++17/-std=c++20/g' "$TRELLIS_HOME/$pb_f" > "$TRELLIS_HOME/$pb_f.tmp"
    mv "$TRELLIS_HOME/$pb_f.tmp" "$TRELLIS_HOME/$pb_f"
  done
  echo "c++20   Metal builds"
  git -C "$TRELLIS_HOME/$SUBMODULE_REPO" submodule update -q --init --depth 1
  echo "ok      $SUBMODULE_REPO submodules"
}

# HF_TOKEN for this run when it is empty: HUGGING_FACE_API_KEY, exported or
# read from its assignment in TRELLIS_ENV_FILE. The file is read, not
# sourced, and a computed value (`$...`) is ignored. Nothing is written.
token_from_env() {
  [ -z "${HF_TOKEN:-}" ] || return 0
  te_key="${HUGGING_FACE_API_KEY:-}"
  if [ -z "$te_key" ] && [ -f "$TRELLIS_ENV_FILE" ]; then
    te_key=$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}HUGGING_FACE_API_KEY=//p' \
      "$TRELLIS_ENV_FILE" | tail -1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")
    case "$te_key" in *'$'*|*'`'*) te_key="" ;; esac
  fi
  [ -z "$te_key" ] || { HF_TOKEN="$te_key"; export HF_TOKEN; }
}

# HF_HUB_CACHE for this run when it is unset: the bulk storage root's
# system/huggingface/hub. No sd here means no root, and Hugging Face's
# default. An sd that fails to answer is unknown, never the default.
# Sets WHY on failure.
cache_from_root() {
  [ -z "${HF_HUB_CACHE:-}" ] || return 0
  [ -x "$SD_BIN" ] || return 0
  cr_root=$("$SD_BIN" config get sd.bulk_storage_root </dev/null 2>/dev/null) || cr_root=""
  if [ -z "$cr_root" ]; then
    WHY="sd.bulk_storage_root did not read; set HF_HUB_CACHE or fix sd"
    return 1
  fi
  HF_HUB_CACHE="$cr_root/system/huggingface/hub"
  export HF_HUB_CACHE
}

# Fails, setting WHY, when HF_HUB_CACHE is on a /Volumes disk that is not
# mounted: a download would land on the system disk under that path.
cache_mounted() {
  case "${HF_HUB_CACHE:-}" in
    /Volumes/?*) ;;
    *) return 0 ;;
  esac
  cm_vol="/Volumes/$(printf '%s\n' "${HF_HUB_CACHE#/Volumes/}" | cut -d/ -f1)"
  if [ -d "$cm_vol" ] &&
     [ "$(df -P "$cm_vol" 2>/dev/null | awk 'NR == 2 { print $NF }')" = "$cm_vol" ]; then
    return 0
  fi
  WHY="Hugging Face cache volume $cm_vol is not mounted (HF_HUB_CACHE=$HF_HUB_CACHE)"
  return 1
}

# ZhengPeng7/BiRefNet's remote code imports einops, which setup.sh does
# not install. Installed after setup.sh, and on the finished path too: a
# record written before this step leaves a venv without it.
ensure_einops() {
  ee_py="$TRELLIS_HOME/.venv/bin/python"
  if "$ee_py" -c 'import einops' </dev/null >/dev/null 2>&1; then
    echo "ok      einops"
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$ee_py" einops </dev/null ||
      die "einops did not install into the venv; rerun install"
  else
    "$ee_py" -m pip install einops </dev/null ||
      die "einops did not install into the venv; rerun install"
  fi
  echo "einops  installed"
}

manual_steps() {
  cat <<EOF

Two manual steps, once per machine (no token is stored here):
  1. hf auth login
  2. accept the licence at $DINO_URL
EOF
}

# Whether a run already finished at these pins: the record matches, every
# checkout is at its pin, the venv is there. einops and the swap are not
# asked: the finished path redoes both, so a record from before either
# step does not rerun setup.sh. Setup then has nothing to do, and
# machine-setup's --apply, which runs install for any status gap, does not
# rebuild the Metal backends for a missing token.
finished() {
  [ -f "$STAMP" ] && pins | cmp -s - "$STAMP" || return 1
  [ -x "$TRELLIS_HOME/.venv/bin/python" ] || return 1
  pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    [ "$(head_of "$(dest_of "$path")")" = "$sha" ] || return 1
    [ "$(git -C "$(dest_of "$path")" remote get-url origin 2>/dev/null)" = "$url" ] || return 1
  done
}

cmd_install() {
  refuse_volume
  token_from_env
  cache_from_root || :
  if finished; then
    echo "ok      setup finished at these pins"
    ensure_einops
    swap_rembg
    record_patches
    manual_steps
    return
  fi
  # The stamp goes first: a run stopped at any later step reads as
  # unfinished in status until a rerun gets through every step.
  refuse_edits
  rm -f "$STAMP"
  pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    checkout_pin "$path" "$url" "$sha"
  done
  pins | grep -v '^[[:space:]]*\(#\|$\)' | while read -r path url sha; do
    [ "$(head_of "$(dest_of "$path")")" = "$sha" ] || die "$path is not at $sha"
  done
  prep_build
  (cd "$TRELLIS_HOME" && bash ./setup.sh) || die "setup.sh failed; rerun install"
  ensure_einops
  swap_rembg
  record_patches
  pins > "$STAMP"
  manual_steps
}

# The probe, run in the venv: `einops yes|no`, `token yes|no`, then with
# a token `gate ok`, `gate gated` (GatedRepoError: licence not
# accepted) or `gate error <exception>`.
PROBE='
import os
try:
    import einops
    print("einops yes")
except ImportError:
    print("einops no")
from huggingface_hub import get_token
token = get_token()
print("token yes" if token else "token no")
if token:
    try:
        from huggingface_hub import auth_check
        from huggingface_hub.utils import GatedRepoError
        try:
            auth_check(os.environ["DINO_REPO"])
            print("gate ok")
        except GatedRepoError:
            print("gate gated")
    except Exception as e:
        print("gate error " + type(e).__name__)
'

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
  if ! swapped; then
    echo "DIFFERS rembg is not swapped to ZhengPeng7/BiRefNet -- run: trellis.sh install"
    st_bad=1
  fi
  token_from_env
  if ! cache_from_root; then
    echo "FAILED  $WHY"
    st_bad=1
  elif ! cache_mounted; then
    echo "MISSING $WHY"
    st_bad=1
  fi
  if [ -x "$st_py" ]; then
    # A failed probe proves nothing either way, so it is FAILED, never ok.
    st_probe=$(DINO_REPO="$DINO_REPO" "$st_py" -c "$PROBE" </dev/null 2>/dev/null) || st_probe=failed
    st_einops=$(printf '%s\n' "$st_probe" | sed -n 's/^einops //p')
    st_tok=$(printf '%s\n' "$st_probe" | sed -n 's/^token //p')
    st_gate=$(printf '%s\n' "$st_probe" | sed -n 's/^gate //p')
    case "$st_einops" in
      yes) ;;
      no) echo "MISSING einops in the venv (BiRefNet imports it) -- run: trellis.sh install"; st_bad=1 ;;
      *) echo "FAILED  einops probe in the venv"; st_bad=1 ;;
    esac
    case "$st_tok" in
      yes) ;;
      no)
        echo "DIFFERS no Hugging Face token -- run: hf auth login, or set HUGGING_FACE_API_KEY"
        st_bad=1 ;;
      *) echo "FAILED  Hugging Face token probe in the venv"; st_bad=1 ;;
    esac
    if [ "$st_tok" = yes ]; then
      case "$st_gate" in
        ok) ;;
        gated)
          echo "DIFFERS DINOv3 licence not accepted -- accept it at $DINO_URL"
          st_bad=1 ;;
        *)
          echo "FAILED  DINOv3 licence probe (${st_gate:-no answer})"
          st_bad=1 ;;
      esac
    fi
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
  token_from_env
  cache_from_root || die "$WHY"
  cache_mounted || die "$WHY"
  if [ -n "${HF_HUB_CACHE:-}" ]; then
    mkdir -p "$HF_HUB_CACHE"
  fi
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
