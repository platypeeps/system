# local-trellis

[TRELLIS.2](https://github.com/microsoft/TRELLIS.2) turns one image into a
textured 3D mesh. This folder installs it on Apple Silicon through the
[trellis-mac](https://github.com/shivampkumar/trellis-mac) port, with every
git checkout pinned by commit.

## Usage

    trellis.sh install                       # clone, pin, set up, swap rembg
    trellis.sh status                        # ok, or what is missing
    trellis.sh generate photo.png --output out/model
    trellis.sh test -v

`generate` runs trellis-mac's `generate.py` in the venv and passes every
option through (`--seed`, `--pipeline-type`, `--texture-size`,
`--no-texture`, `--steps`). `--output` takes a path without extension; the
`.glb` and `.obj` land there.

## What install does

1. Clones trellis-mac into `~/.local/share/trellis-mac` (`TRELLIS_HOME`)
   at `d58628f`. A path under `/Volumes` is refused: the venv and the
   native builds stay on the system disk.
2. Clones every repository trellis-mac's `setup.sh` would clone, each at
   the commit in `pins()` in `trellis.sh`: `deps/utils3d`, `deps/mtlbvh`,
   `deps/mtldiffrast`, `deps/mtlmesh`, `deps/mtlgemm`,
   `deps/trellis2-apple` and `TRELLIS.2`. `setup.sh` clones them at their
   tip only when the folder is missing, so it skips all of them.
3. Prepares the Metal builds. `setup.sh` installs torch 2.15, which
   needs C++20, and the five `setup.py` files of `mtlbvh`,
   `mtldiffrast`, `mtlmesh`, `mtlgemm` and `trellis2-apple/o-voxel` pass
   `-std=c++17`; install changes each to `-std=c++20`. It also fetches
   `trellis2-apple`'s submodules (`o-voxel/third_party/eigen`), at the
   commit the pin records.
4. Runs `setup.sh`: a Python 3.11 venv, PyPI packages, the Metal backends
   and the MPS patches. The PyPI packages are not pinned; only git
   checkouts are.
5. Installs `einops` into the venv (`uv pip install`), since
   ZhengPeng7/BiRefNet's remote code imports it and `setup.sh` does not
   install it. A rerun after a finished setup still adds it.
6. Swaps background removal. TRELLIS.2's `pipeline.json` on Hugging Face
   (`microsoft/TRELLIS.2-4B`) names `briaai/RMBG-2.0`, whose licence is
   non-commercial. Both pipelines build that model through one class,
   `trellis2/pipelines/rembg/BiRefNet.py`, so install adds a two-line
   guard at the top of its `__init__` that maps `briaai/RMBG-2.0` to
   `ZhengPeng7/BiRefNet` (MIT, the same architecture). Those weights are
   fp16 and the input fp32, so it also adds `self.model.float()` right
   before `self.model.eval()`. The swap counts as done only with both; a
   guard and cast of that shape added by hand count. A pin whose `BiRefNet.py` no longer
   has that `__init__` fails the install instead of skipping the swap.
7. Writes `.local-trellis-installed`, the pin list it finished with.

Rerun install at any time: it removes that record first, moves each
checkout whose pin changed, and writes the record again only after every
step passed. A stopped run reads as `setup not finished` in `status`.

A pin moves only over changes install made itself. Install records each
checkout's HEAD and the hash of its `git diff HEAD` in
`.local-trellis-patches`: the C++20 edit, the rembg guard and cast, and
`setup.sh`'s MPS patches. It records a checkout only when that checkout
was clean or matched its line as the run began, and it records on every
exit, a failed run's too. A checkout holding an edit install did not make
keeps its old line, so the edit never counts as a patch. Before any pin
moves, each checkout whose pin moves must be clean or match its line. Any other tracked change stops
the install, naming each file, and no pin moves. A matching checkout is
reset, moved and patched again. git never replaces an untracked file in
the way, and a replaced `origin` URL is printed.

## Models and token at run time

The model weights, about 15 GB, download on the first `generate` to
`HF_HUB_CACHE`. When it is unset, every run sets it to
`<sd.bulk_storage_root>/system/huggingface/hub`, an ejectable SSD; the venv
and the native builds stay on the system disk. Without `sd` the cache is
Hugging Face's default. When `sd` does not answer, or the cache's
`/Volumes` disk is not mounted, `generate` stops and `status` says so,
instead of downloading to the system disk.

When `HF_TOKEN` is empty, each run takes it from `HUGGING_FACE_API_KEY`,
exported or assigned in `~/.config/shell/env.sh` (`TRELLIS_ENV_FILE`). The
file is read, not sourced, and the token is written nowhere.

## Two manual steps

The script stores no Hugging Face token. Once per machine:

1. `hf auth login`, unless `HUGGING_FACE_API_KEY` holds a token
2. Accept the licence at
   https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m

`status` reports both: a missing token, and the licence when Hugging Face
answers `GatedRepoError` for that repository.

## Status

`status` prints `ok` or one line per gap: `MISSING` for no checkout, no
venv, no `einops` or an unmounted cache disk, `DIFFERS` for a checkout off its pin, an
unfinished setup, a rembg not swapped, no token or the DINOv3 licence not
accepted, `FAILED` when a probe itself fails: `einops`, the token, the
licence or `sd.bulk_storage_root`. It exits 0
when all is in place, 3 when nothing is installed and 1 otherwise.
`local-health-check` reads those codes.

## Moving a pin

Edit the commit in `pins()`, then run `trellis.sh install`. Read the new
`setup.sh` first: a new clone it adds needs a pin line, and a moved guard
path needs its line changed.
