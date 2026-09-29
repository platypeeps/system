# Writing style: STE-Concise

Use STE-Concise for replies and authored prose unless the user requests another style.
Use active voice and simple tenses: infinitive, imperative, simple present, past, and future.
Limit prose sentences to 20 words.
Express one idea or instruction per sentence.
Lead with the answer or outcome.
Omit filler, conversational preambles, and closing summaries.
Keep exact technical identifiers, file paths, and code blocks unchanged.
Preserve accuracy, actionable failures, and necessary caveats when brevity conflicts with them.

# Instruction files

CLAUDE.md, AGENTS.md and `.claude/rules/` hold rules, not history.
Record incidents, findings and migration stories in `docs/`.
Keep a rule to one line with a short reason.
Keep a CLAUDE.md under 200 lines; move area rules to `.claude/rules/` with `paths:`.

# Diagrams

Use Archify when available for architecture, workflow, sequence, data-flow, and state diagrams.
Create a diagram only when it clarifies an important relationship.
Use a short table or prose when either explains the relationship clearly.
Do not add decorative artifacts.
If Archify is unavailable, name the limitation and use a suitable available format.

# RTK - Rust Token Killer

A hook rewrites shell commands through `rtk`, a token-saving CLI proxy: `git status` becomes `rtk git status`.
Run `rtk` meta commands such as `rtk gain` directly. If `rtk gain` fails, reachingforthejack/rtk (Rust Type Kit) may be installed instead.

If `rtk` misbehaves, re-run the command through `rtk proxy <cmd>` — that
bypasses the filtering, so identical output means the wrapper is at fault and
different output means the command itself is. If the wrapper is at fault, fall
back to the bare command and say so; do not keep retrying through `rtk`.

# GitHub: MCP before `gh`

For GitHub work — PRs, reviews, commits, repo contents, releases — use the `mcp__github__*` tools, not `gh` via Bash.

**We do not use GitHub issues.** Track work in the `sd` tracker: `sd task add`, `sd task note`, `sd task status`.
Never open, search or cite a GitHub issue as the work record; pack repos have issues disabled.

These tools are usually **deferred**: only their names are loaded, so they look unavailable and `gh` looks like the only option. It isn't. Load schemas first, batching everything the task needs into one call:

`ToolSearch("select:mcp__github__pull_request_read,mcp__github__list_pull_requests,mcp__github__get_commit")`

Then call them normally. One extra round-trip buys structured JSON and field selection instead of parsing CLI text.

Use `gh` when no MCP equivalent exists, or when the MCP server is disconnected or unavailable.
Examples include `gh run watch`, `gh pr checkout`, and workflow dispatch without an equivalent loaded MCP tool.
Check available tools and connection status before declaring MCP unavailable; Claude Code provides `claude mcp list`.
Use Git for local repository state.
Private repositories follow the same MCP-first rule as public repositories.

**Use a classic PAT with `repo` scope for the MCP server.** A fine-grained PAT is scoped per owner and per repository, so private repositories in other organizations return 404. A classic PAT reaches every SSO-authorized organization.

**Token changes need a Claude Code restart.** The server authenticates with `Authorization: Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}`, interpolated from the process env inherited at launch — editing your shell env file does not reach a running session, and `/mcp` reconnect reuses the same stale value. Tell-tale: MCP 404s on a repo that `curl` with the same token reads fine. Quick health check: `search_repositories("is:private")` — `total_count: 0` means the session is holding a stale or under-scoped token.

# Verification

Applies to work that produces or changes something — code, files, config, or a factual claim about the state of a system. Not to conversation, opinions, or questions answered from what is already on screen.

## Before starting: name the check

State the specific check that would catch you being wrong, and what result means failure. Before the work, not after — a check chosen afterward is chosen to pass.

Falsifiable: *"`pytest tests/auth` — 3 failing tests should pass, 0 new failures."* / *"`grep -c` the pattern vault-wide — expect 0; any hit means a case was missed."*

Not: *"I'll review the code"*, *"make sure it works"*, *"check the output looks right."*

**If you cannot verify it — it depends on an external service, a human decision, or the user's eyes — say so instead of inventing a check.** Name what you can verify, what you cannot, and what would settle the rest. A stated gap is useful; a fabricated verification is worse than none.

## After finishing: run it and report

Run the check you named. Quote the shortest decisive line of actual output. A partial pass is not a pass.

- Check changed mid-task: say why. One swapped for an easier one after the fact verified nothing.
- Never ran it: say that. *"Not verified"* is a legitimate report. *"Verified"* when nothing ran is not.

## Scope the check to the blast radius, not to what you edited

The most common way a check passes while the work is wrong: it covers the files you touched
instead of everywhere the thing you changed appears. **Enumerate first, then check.**

- **Renaming or replacing a name** — grep every repo that writes it, not the directory you are
  in. A rename that stops at a repo boundary leaves the old name live somewhere that reads it.
- **Swapping a tool, library, or transport** — three things change, not one: what it is
  *called*, what *authorizes* it (grants, config, allow-lists), and what it can *do*. The third
  is the one nobody looks for: check what the docs still claim is impossible.
- **Adding or removing a thing that gets listed** — find the inventories. Anything reciting a
  list drifts silently; prefer making it enumerate at runtime over correcting the list.
- **Correcting a stored fact** — find every store that derives from it. A row, its search
  index, a sync queue, a vector embedding: fixing one and asserting "corrected" is wrong three
  times over.

The passing form of the check enumerates from the filesystem or the database — `ls`, a schema
query, a repo-wide grep — rather than searching for the string you just typed. A check built
from what you already know cannot find what you did not know about.

# CI runs locally, not on GitHub Actions

GitHub Actions CI is off on purpose since 2026-09-27: billing blocked every run.
Do not re-enable Actions or CI workflows to get a missing check; `sd/local-gate` is the required check.
Read `repo.ci` with `~/repos/system/local-sd-db/sd-db.sh repo list`; `local` means `sd-ship merge` runs `sd-check`.
Dependabot stays on; land its pull requests through `sd-ship` like any other, since GitHub cannot post `sd/local-gate`.
Who, why, scope, and how to revert: `~/repos/system/docs/local-ci-rollout.md`.

# sd lane commands: `-C <dir>`, not `cd <dir> &&`

To run `sd-ship`, `sd-check`, `sd-review`, `sd-review-ack` or `sd-pr-state` in another checkout, write `<command> -C <dir> …`.
Reason: an allow rule matches the whole line, so `cd <dir> && sd-ship merge …` goes to the auto-mode classifier, which denies it.
Put `-C` first, before the subcommand: `sd-ship -C ~/repos/system merge --item N …`.
Give one `-C` with an absolute path; a second `-C` or a `..` component is refused.
Only these five commands take `-C`; `sd` and `sd-docs-lint` do not, so run those from the checkout.

# Git worktrees go outside `~/repos`

Create a git worktree outside `~/repos`, for example `~/worktrees/<repo>-<branch>`: `repo-sync` reads each folder under `~/repos` as a checkout.
