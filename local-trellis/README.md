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
3. Runs `setup.sh`: a Python 3.11 venv, PyPI packages, the Metal backends
   and the MPS patches. The PyPI packages are not pinned; only git
   checkouts are.
4. Swaps background removal. TRELLIS.2's `pipeline.json` on Hugging Face
   (`microsoft/TRELLIS.2-4B`) names `briaai/RMBG-2.0`, whose licence is
   non-commercial. Both pipelines build that model through one class,
   `trellis2/pipelines/rembg/BiRefNet.py`, so install adds one line at the
   top of its `__init__` that sets `model_name` to `ZhengPeng7/BiRefNet`
   (MIT, the same architecture). A pin whose `BiRefNet.py` no longer has
   that `__init__` fails the install instead of skipping the swap.
5. Writes `.local-trellis-installed`, the pin list it finished with.

Rerun install at any time: it removes that record first, moves each
checkout whose pin changed, and writes the record again only after every
step passed. A stopped run reads as `setup not finished` in `status`.

The model weights, about 15 GB, download to the Hugging Face cache on the
first `generate`.

## Two manual steps

The script stores no Hugging Face token. Once per machine:

1. `hf auth login`
2. Accept the licence at
   https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m

`status` reports a missing token; it cannot see the licence.

## Status

`status` prints `ok` or one line per gap: `MISSING` for no checkout or no
venv, `DIFFERS` for a checkout off its pin, an unfinished setup, a rembg not
swapped or no token, `FAILED` when the token probe itself fails. It exits 0
when all is in place, 3 when nothing is installed and 1 otherwise.
`local-health-check` reads those codes.

## Moving a pin

Edit the commit in `pins()`, then run `trellis.sh install`. Read the new
`setup.sh` first: a new clone it adds needs a pin line, and a moved guard
path needs its line changed.
