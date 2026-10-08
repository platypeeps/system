# local-agent-prompt

One shared system prompt, distributed into every coding agent's **global**
instructions file. Source of truth is `prompt/shared.md` in this folder.

Linked onto `PATH` as `agent-prompt` by `local-bin-links`, so it runs from
anywhere; the examples below use the in-folder form.

```sh
./agent-prompt.sh status              # is the block present and current, per target
./agent-prompt.sh refresh --apply     # write it everywhere
./agent-prompt.sh diff                # what each target's block differs by
./agent-prompt.sh capture --apply     # pull an in-place edit back into the repo
./agent-prompt.sh targets             # resolved paths
```

Dry run without `--apply`, like everything else in this repo.

## Shared defaults

The shared block includes STE-Concise writing rules and the Archify diagram preference.
These rules apply to both Claude and Codex, plus the other configured targets.
The block defines the writing rules directly; it requires no separate output-style file.
Archify remains optional when unavailable, and simple relationships need no diagram.
The GitHub rule prefers MCP for public and private repositories.
Use `gh` for missing equivalents or an unavailable MCP connection.

## Verification

Run these checks from the repository root:

```sh
sh -n local-agent-prompt/agent-prompt.sh
sh local-agent-prompt/agent-prompt.sh test -v
sd-docs-lint
```

The component tests use a copied source and isolated home.
They check every discovered target, byte preservation, dry-run behavior, and conflict refusal.
The native CI tools leg runs the component suite.
Run `./agent-prompt.sh refresh` from this folder to preview actual target changes without writing them.
Back up the listed targets and hash records before an approved `refresh --apply`.
After applying, run `./agent-prompt.sh status` and confirm each expected target reports `ok`.
Do not use `--force` to resolve a conflict without separate approval.

## Targets

| Tool | File | Source |
| --- | --- | --- |
| Claude Code | `~/.claude/CLAUDE.md` | — |
| Codex | `~/.codex/AGENTS.md` | — |
| Gemini CLI | `~/.gemini/GEMINI.md` | — |
| Antigravity | `~/.gemini/GEMINI.md` | [antigravity.google/docs/rules-workflows](https://antigravity.google/docs/rules-workflows/) |
| Copilot CLI | `~/.copilot/copilot-instructions.md` | [docs.github.com](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions) |
| OpenCode | `~/.config/opencode/AGENTS.md` | — |

**Six tools, five files.** Antigravity reads the same `~/.gemini/GEMINI.md` as
the Gemini CLI, so it has no target of its own — listing it twice would write
one file twice and double-count its drift.

Copilot CLI is the one that is *not* `AGENTS.md`: it reads
`~/.copilot/copilot-instructions.md` (plus `~/.copilot/instructions/**/*.instructions.md`),
and per GitHub's docs `AGENTS.md` is repository-level only, never global.
`COPILOT_HOME` moves that directory if it is ever set, and the target list
honors it.

## Tools that are not installed

A target whose tool is absent is reported `SKIP` and never written — otherwise
a terra machine ends up with config directories for tools it does not have.

Presence is decided three ways, because no single one is enough:

1. the CLI is on `PATH`;
2. **Antigravity is a GUI app with no CLI**, and it reads the gemini target —
   so `gemini` missing from `PATH` does not make `~/.gemini/GEMINI.md` moot.
   This machine is exactly that case: no `gemini` binary, `Antigravity.app`
   installed;
3. the config directory already exists, covering a tool installed outside
   `PATH`.

Checking only (1) would quietly stop feeding a tool that is very much present.

## The managed block

Content is written between markers:

```
<!-- shared-prompt:start -->
...
<!-- shared-prompt:end -->
```

The marker style matches what these files already use — `~/.codex/AGENTS.md`
carries a `codebase-memory-mcp:start` block, `~/.config/opencode/AGENTS.md` a
`caveman-begin` one.

**Everything outside the markers is preserved byte for byte.** That is the
whole point: these are not files this repo owns. `~/.codex/AGENTS.md` holds 9KB
of hand-written prompt that has nothing to do with the shared block, and a
refresh must leave every byte of it alone. Without markers, the block is
appended; with them, only the region between them is replaced.

## Editing

Edit `prompt/shared.md`, then `refresh --apply`.

If you edit a prompt in place instead — Claude Code's `/memory` writes
`~/.claude/CLAUDE.md` directly — `status` reports `DIFFERS` and
`capture --apply` pulls it back. `capture` refuses a target with no markers
rather than guessing which part of the file is the shared part.

**`refresh` never overwrites a block that was edited in place.** It reports
`DIFFERS` and exits 1 before writing, the same way the `dotfiles` stage in
`local-machine-setup` refuses to overwrite a file the machine and repo disagree
on. Discarding the edit takes `refresh --force`. This matters because the
`prompts` stage calls `refresh --apply` on every `machine-setup update --apply`,
and without the guard a routine provisioning run would silently delete a
`/memory` edit made ten minutes earlier.
Refresh checks every target first and reports all known conflicts.
Any conflict or read failure prevents that pass from writing targets or hash records.
Each target is checked again before its write.
This is not a transaction across targets.
A concurrent change or later write failure can still leave earlier targets updated.
Run `refresh` first; resolve each refusal before applying changes.

It now knows which side moved. Comparing the installed block against
`shared.md` alone cannot tell an edited target from a moved source, and the
tool used to guess — `status` called the condition `STALE` ("differs from
shared.md") while `refresh` called the identical condition `DIFFERS` ("edited
in place"). One of the two was always wrong, and editing `shared.md` meant
forcing every target.

Each write records the SHA256 of the exact managed bytes, including markers, in
`$AGENT_PROMPT_STATE` (default `~/.config/agent-prompt/<target>.sha`).

Managed CRLF and LF bytes are different; comparison does not normalize them.
Unmanaged content can contain non-UTF-8 bytes.
Read failures or invalid marker pairs stop processing; they do not record empty-content hashes.

| state | means | refresh |
|-------|-------|---------|
| `STALE` | target matches what was written; `shared.md` moved | writes it, no `--force` |
| `DIFFERS` | target no longer matches what was written — edited in place | refused; exit 1 |
| `UNKNOWN` | differs, and nothing was recorded | refused; exit 1 |

`UNKNOWN` is what a machine provisioned before the hash existed reports once.
The first `refresh --apply` records a hash for every target, including the ones
already `current` — without that an in-sync machine would never take the write
path, hold no hash at all, and answer `UNKNOWN` on the first ordinary source
edit. `capture --apply` records one too, so a target is not asked to `--force`
its own content back.

## Duplicate sections

`status` also reports a heading that appears **both** inside the block and
outside it. That means the target carries a stale hand-written copy of a
section the block now owns, and the refresh cannot remove it — that content
sits outside the markers, so it is not ours to delete.

`~/.gemini/GEMINI.md` was the first real case: it held an older, drifted copy
of the RTK section claiming "up to 90%" where the shared prompt says "60-90%",
and pointing readers at a `CLAUDE.md` that does not exist for Gemini. It also
carried two things the shared prompt lacked — the Installation Verification
block and the `reachingforthejack/rtk` name-collision warning.

Resolving it is the pattern to follow: **check what the outside copy has that
the block does not, fold that into `prompt/shared.md`, then delete the copy.**
Deleting first would have lost both of those. The file is now block-only.

## Claude rule files

`shared.md` holds the user-level rules, so every machine and every agent gets them.
A file in `~/.claude/rules/` is a second copy that reaches only the machine it sits on.
`status` reports each `~/.claude/rules/*.md` as `DIFFERS`, so the machine-setup drift count sees it.
Fold anything the file has that `shared.md` lacks into `shared.md`, then delete the file.
Personal lines for one machine stay below the block in `~/.claude/CLAUDE.md`.

## RTK is inlined, not imported

`~/.claude/CLAUDE.md` used to start with `@RTK.md`, importing
`~/.claude/RTK.md`. That import syntax is Claude Code's alone — no other tool
here resolves it — so `prompt/shared.md` carries the RTK section inline
instead. `~/.claude/RTK.md` has been deleted: its content is in the block, and
nothing in live config referenced the file (the only remaining hits were
session transcripts and `file-history/` snapshots).

The section used to end by pointing at `~/.claude/docs/rtk-troubleshooting.md`.
That file does not exist on this machine, `~/.claude/docs/` does not exist
either, and it has never been in this repo — so the block was writing a
dangling pointer into all five targets. The rule now is the one this repo can
keep true: **the shared prompt names only files this repo installs.** The
troubleshooting line was replaced with the `rtk proxy` bisect it would have
told you to run, which needs no file at all.

## machine-setup

The `prompts` stage in `local-machine-setup` runs `status` on a dry run and
`refresh --apply` for real, so a new machine gets the prompt everywhere without
a separate step.
