# local-bin-links

Puts this repo's command-line tools on `PATH` by symlinking them into
`~/bin/common`. Run this on a new machine after cloning the repo.

## Usage

```sh
./bin-links.sh install    # create the symlinks (safe to re-run)
./bin-links.sh status     # what is linked, and whether the dir is on PATH
./bin-links.sh remove     # delete only the links pointing into this repo
```

Linked tools:

| Link | Target |
| --- | --- |
| `repo-sync` | `local-repo-sync/repo-sync.sh` |
| `gito` | `local-gito/gito.sh` |
| `prism` | `local-prism/prism.sh` |
| `mac-utils` | `local-mac-utils/mac-utils.sh` |
| `notify` | `local-notify/notify.sh` |
| `agent-prompt` | `local-agent-prompt/agent-prompt.sh` |
| `adversarial-gate` | `local-adversarial-gate/adversarial-gate.sh` |
| `ha-mcp` | `local-ha-mcp/ha-mcp.sh` |
| `llama-cpp` | `local-llama-cpp/llama-cpp.sh` |

**The `platypeeps/sd-ai-command-pack` commands are not linked here.** The pack's
installer (`python3 bin/sd_install.py --user`) links `sd`, `sd-status` and the
rest into `~/.local/bin`. A link this script made earlier into the pack checkout
shadows that install (`sd_install.py --verify` reports `command_shadowed`), or
dangles once the pack drops the command. `install` deletes such a link and
prints each removal, but only when the installer's copy of the same name in
`~/.local/bin` runs on its own and that directory is on `PATH`, or when the link
dangles. Runs on its own means an executable regular file whose symlink chain
never passes through the bin dir being swept; a directory, a loop or a link back
into `~/bin/common` does not count. Otherwise it keeps the link, because it is
then the only working copy, and says `kept pack link <name>: ...`; run `make setup` in the pack. `status`
reports a removable link `STALE` and a kept one `MISSING` (both words
machine-setup's drift count reads). `remove` deletes every such link on request.
Only a symlink whose target is inside the checkout (`SD_PACK_ROOT`, default
`~/repos/platypeeps/sd-ai-command-pack`) and names a regular file is touched;
a regular file in the bin dir never is.
The installer's directory is read as `~/.local/bin`; a machine that installs the
pack with `--bin-dir` keeps its old links until `remove`.

`./bin-links.sh test` runs `tests/`, against a fake pack and bin dir.

`install` is idempotent: a correct link is left alone, a link pointing at the
wrong target is retargeted, and a **real file** in the way is skipped with a
message rather than overwritten. `remove` only deletes links that resolve into
this repo, so anything you hand-wrote survives.

Dropping a row from the table cannot clean up the link it used to make — a row
that is gone names nothing. So retired links are listed separately and swept by
`install` and `remove`, and reported by `status`. A listed name is touched
only while its link points into this repo; a link of that name pointing
anywhere else is somebody else's and is left alone. A row leaves once every
machine has run `install` past the change. The list is empty today: its one
row, `research-kit`, left in sd:1156.

## Setting up a new machine

1. **Clone the repo.** The tools resolve their own location, so any path works;
   `~/repos/system` is used below.

   ```sh
   git clone git@github.com:platypeeps/system.git ~/repos/system
   ```

2. **Link the tools.**

   ```sh
   ~/repos/system/local-bin-links/bin-links.sh install
   ```

3. **Put `~/bin/common` on PATH** if `install` says it is not. In `~/.zshrc`:

   ```sh
   path+=("$HOME/bin/common")
   ```

   or in `~/.bash_profile`:

   ```sh
   export PATH="$HOME/bin/common:$PATH"
   ```

4. **Install per-tool prerequisites** — see the table below. Nothing here is
   needed for the tools you do not use; each one fails with a clear message
   naming what is missing.

5. **Wire the statusline** (optional, not a `~/bin` link):

   ```sh
   ~/repos/system/local-statusline/statusline.sh install
   ```

## Per-tool prerequisites

| Tool | Needs | Notes |
| --- | --- | --- |
| `repo-sync` | `git`, an SSH key on GitHub | Clones over `git@github.com:`. Run `mac-utils addkey` once; it keeps the passphrase in the keychain, so `repo-sync nightly` can load the key after a reboot. Check the machine profile with `repo-sync list` before the first `repo-sync sync`. |
| `gito` | [`uv`](https://docs.astral.sh/uv/) (for `uvx`) | Credentials go in `~/.gito/.env`, or export `OPENAI_API_KEY`. Run `gito setup` for the interactive path. |
| `prism` | a checkout at `~/repos/ai/prism` with a built `prism` binary | Override the location with `PRISM_BIN`. Config in `~/.prism/.env`. |
| `mac-utils` | macOS | `flushdns` and `resetvideo` call `sudo`. |
| `notify` | `curl`, macOS | ntfy channel needs `NTFY_TOPIC` — see `local-notify/.env.example`. |
| `ha-mcp` | `npx` (Node), `HA_TOKEN` | Bridges Home Assistant's MCP endpoint to stdio. `HA_TOKEN` normally comes from `~/.config/shell/env.sh`; `ha-mcp check` verifies the connection. |
| `llama-cpp` | `cmake`, a checkout at `~/repos/ai/llama.cpp` (repo-sync) | `llama-cpp build` compiles it with Metal + RPC. `rpc` and `cluster` need the Thunderbolt bridge IPs from `network-testing`. |

## Gotchas

- Secrets stay out of this repo: `~/.gito/.env` and `~/.prism/.env` are read at
  runtime from `$HOME` and are never tracked here.
- `repo-sync` picks its profile automatically — the profile machine-setup
  recorded, else `work` if `REPO_SYNC_WORK_ROOT` (or `SYSTEM_TOOLS_WORK_ROOT`)
  names an existing directory, else `personal`. On a new machine that is
  neither, set `REPO_SYNC_PROFILE` and `REPO_SYNC_ROOT`, or add a new
  `~/.config/system/repo-sync/repos.<profile>.conf`.
- Private config for every linked tool lives in `$SYSTEM_TOOLS_CONFIG/<tool>/`
  (default `~/.config/system/<tool>/`), not beside the script, so a link needs
  nothing copied next to it.
- The link names deliberately have no `.sh` suffix, matching what these tools
  were called when they lived loose in `~/bin/common`.
