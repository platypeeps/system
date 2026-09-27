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
| `sd`, `sd-status`, `sd-review`, … | `$SD_PACK_ROOT/bin/<same name>` |

**Every command the `platypeeps/sd-ai-command-pack` checkout ships is linked**,
and they are not listed here or in the script: `bin-links.sh` reads the pack's
`bin/` on each run. The rule is the one the pack's `sd_install.py --status`
counts with — `sd*`, no extension, a regular executable file — so the
`sd_*.py` modules beside them are skipped. The pack's installer renders skills
and hooks and links no executable, so this is what puts `sd-status` or
`sd-handoff` on `PATH`. Its `--status` line still reads `not on PATH`
afterwards: it asks whether the pack's `bin/` directory itself is on `PATH`,
which links do not make true. What it does check is that nothing competes —
a `[N shadowed by another install]` suffix would mean some other copy of a
command wins. `SD_PACK_ROOT` defaults to
`~/repos/platypeeps/sd-ai-command-pack` and a missing checkout is a `SKIP`,
not a failure.

A command the pack stops shipping leaves a link to nothing, and no row names
it. `status` reports such a link `STALE` — a word machine-setup's drift count
reads — and `install` and `remove` delete it. Only a dangling link whose target
is inside the pack's `bin/` is touched.

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
   `~/repos/system-tools` is used below.

   ```sh
   git clone git@github.com:platypeeps/system-tools.git ~/repos/system-tools
   ```

2. **Link the tools.**

   ```sh
   ~/repos/system-tools/local-bin-links/bin-links.sh install
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
   ~/repos/system-tools/local-statusline/statusline.sh install
   ```

## Per-tool prerequisites

| Tool | Needs | Notes |
| --- | --- | --- |
| `repo-sync` | `git`, an SSH key on GitHub | Clones over `git@github.com:`. Run `mac-utils addkey` first if the agent is empty. Check the machine profile with `repo-sync list` before the first `repo-sync sync`. |
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
  recorded, else `work` if `REPO_SYNC_WORK_ROOT` names an existing directory,
  else `personal`. On a new machine that is neither, set
  `REPO_SYNC_PROFILE` and `REPO_SYNC_ROOT`, or add a new `repos.<profile>.conf`.
- The link names deliberately have no `.sh` suffix, matching what these tools
  were called when they lived loose in `~/bin/common`.
