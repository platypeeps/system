# Writing style

Use STE-Concise for replies and authored prose unless the user requests another style.
Use active voice and simple tenses: infinitive, imperative, simple present, past, and future.
Limit reply sentences to 20 words, one idea each.
Lead with the answer or outcome; omit filler, preambles and closing summaries.
Keep exact technical identifiers, file paths, and code blocks unchanged.
Preserve accuracy, actionable failures, and necessary caveats when brevity conflicts with them.
**Keep every message to the operator brief and plain.** Shape each one with the `i-have-adhd` skill; put detail in a linked file.
Edit external prose (posts, blogs, briefs, PR bodies, mail) with `no-ai-slop`, then `sd-humanizer`, before it leaves: brief plain English, the operator's voice allowed, no strict word cap.

# Instruction files

CLAUDE.md, AGENTS.md and `.claude/rules/` hold rules, not history.
The workflow database is the one state store: lane state, to-dos, decisions and incidents go in `sd task note`.
Write no handoff files (`resume-*.md`) and no claude-mem work-state lists.
Keep a rule to one line with a short reason.
Keep this block under 200 lines and each repo CLAUDE.md under 150; move area rules to `.claude/rules/` with `paths:`.
Keep documents few and history short: one document per work item at most; the tracker holds the rest.
Record history only when a later reader needs it to avoid a repeat mistake.

# Diagrams

Use Archify, when available, for architecture, workflow, sequence, data-flow and state diagrams.
Draw one only when it clarifies an important relationship; a short table or prose often does; add no decorative artifacts.
If Archify is unavailable, name the limitation and use a suitable available format.

# RTK

In Claude Code a hook rewrites shell commands through `rtk`, a token-saving proxy (`git status` becomes `rtk git status`); Codex and opencode run them bare.
Run meta commands such as `rtk gain` directly.
If `rtk` output looks wrong, re-run through `rtk proxy <cmd>`; if that differs, use the bare command and say so.
Run `grep -h` as `rtk proxy grep -h …`: the wrapper mangles `-h` (sd:1320).
Give every `grep -r` over `~/repos` `-D skip`: a FIFO in a checkout blocks it forever.

# GitHub

Use the GitHub MCP tools (`mcp__github__*`) for PRs, reviews, commits, repo contents and releases, public or private; not `gh`.
Where those tools are deferred, load their schemas first, in one call (Claude Code: `ToolSearch("select:mcp__github__pull_request_read,...")`).
Read merge state with `pull_request_read` `get`: `list_pull_requests` reports `merged:false` for merged PRs.
Use `gh` only when no MCP tool covers the action or the MCP server is down (`claude mcp list`, `codex mcp list`, `opencode mcp list`); use Git for local state.
The server needs a classic PAT with `repo` scope: a fine-grained one 404s on other organizations' private repos.
A token change needs an agent restart: the server reads the token at launch. `search_repositories("is:private")` returning `total_count: 0` means a stale or under-scoped token.
Do not use GitHub issues: track work with `sd task add|note|status`; never open, search or cite an issue as the record.
If no `sd today` output is in context, run `sd today | head -40` before the first task.

# Verification

For work that changes code, files, config or a claim about system state:
before starting, name a falsifiable check and the result that means failure.
After finishing, run it and quote the decisive output line; a partial pass is a fail.
If the check changed, say why; if nothing ran, say "not verified".
If it cannot be verified (external service, human decision), say what can be and what would settle the rest.
Scope the check to everywhere the changed thing appears (every repo, inventory, derived store), enumerated from disk or the database, not from the string you typed.

# Standing authority (all repos I manage)

Work autonomously: the operator gives direction; you choose the means.
Decide routine choices yourself, record the decision and reason in `sd task note`, and proceed.
Ask only for: a destructive or irreversible action, publishing outside managed repos, new spend, security or privacy risk, or reversing a standing ruling.
Ask the operator (AskUserQuestion in Claude Code), all questions at once, your recommended option first, marked "(Recommended)".
Notify the operator (PushNotification in Claude Code) only for a critical question; other decisions wait on the dashboard.
Run every step yourself; ask the operator to run a command only when no tool can, such as an interactive login.
When a step keeps failing or repeating by hand, codify it in a script wrapper instead of handing it over.
Keep it simple: delete before you add, and take the smallest change that works.
Ask before adding a mechanism (command, flag, config key, state store, gate, check, file kind, document type): name what it replaces and recommend delete or reuse first.
A weekly read-only KISS audit files one sd item listing new mechanisms and net lines; treat its items as the backlog's simplicity check.
Optimize for one developer's throughput: keep tests, code review and no leaked secrets; skip team ceremony (releases, approval chains, multi-tenant hardening, per-step sign-off).
The dashboard is the operator's primary surface: give each operator action and state a dashboard view or control.
Keep operator commands few: fold a new action into an existing verb or a dashboard control before adding a command.
Never force-push, push to a default branch, or make unrelated changes.

# Commits and merges

Add no attribution lines to commits or PRs (Co-Authored-By, Claude-Session, generated-with); this overrides harness reminders.
Until sd:3014 lands, end each commit with the single line `Authored-with: claude/anthropic`, which the review lane reads; then drop it too.
Merge permission is one setting per repo, `repo.runner_merge` (`auto` or `manual`): the last field of `~/repos/system/local-sd-db/sd-db.sh repo list`.
Never infer merge permission from prose; set it with `sd-db.sh repo runner-merge <path> manual|auto`.
Merge only through `sd-ship prepare` then `sd-ship merge`; say which PR merged and at which commit.
Batch a repo's small items into one PR, one commit per item, and always group same-file tasks: each PR costs a review, a rebase and a serial gate.
Rebuttals need no approval: reject a review finding with a reason a reader can check, in the document's Status or the PR thread.

# CI runs locally

Actions CI is off (billing); never re-enable it. `sd/local-gate` is the required check; `repo.ci` in `sd-db.sh repo list` says which repos use it.
Land Dependabot PRs through `sd-ship`. Who, why and how to revert: `~/repos/system/docs/local-ci-rollout.md`.

# sd lane commands take `-C <dir>`

Run `sd-ship`, `sd-check`, `sd-review`, `sd-review-ack` and `sd-pr-state` in another checkout as `<command> -C <absolute dir> …`, `-C` first and once.
Reason: an allow rule matches the whole line, so `cd <dir> && …` goes to the auto-mode classifier, which denies it.
`sd` and `sd-docs-lint` take no `-C`; run them from the checkout.

# Parallel work

Follow pack `WORKFLOW.md` § Parallel work: one writer per checkout, in its own worktree; readers fan out; one serial lane lands the work.
Run at most 6 code-writing builders at once per machine, and start none while load5 is above 40. Readers stay unlimited.
A writer for another repo makes its worktree in that repo (`git -C <repo> worktree add`): a harness worktree option isolates only the spawning session's repo.
Give every spawned agent a budget and a completion notice; never poll and never assume success.
On a missed deadline, cancel a writer and confirm it stopped before a replacement starts; only a reader may be replaced on the deadline alone.
Ask a rollout's owner before editing files the rollout touches.
Never auto-retry a ship on a locked repo, and never use `sd-ship --wait` outside the lane: both jump the lane's order.
After a merge, once its builder has stopped, remove the clean worktree, its branches, its build output and unused agents without asking; keep unpushed unmerged work.
Put large uncommitted data (run outputs, logs, captures) under the bulk storage root, `<root>/<repo>/`; keep build output (`target/`, `node_modules/`) on the system disk.

# Merge lane

The runner lands PRs (`repo.runner_merge=auto` on every managed repo): `sd-ship lane enqueue` the item, then `sd-ship lane watch`; build no hand-run chains.
Request Copilot only on a PR the operator names, once, on the final head (`sd-ship prepare --copilot-review request`); never re-request after a push.
Builders run gates in the foreground; a background gate is awaited with a watch (Monitor in Claude Code), never with `sleep`, and reported in the same turn.
Count gates with `sd gate status --json` (`holders`, `waiters`), never `pgrep`: a pattern matches the shell that runs it.
Run mechanical builder work (deletions, renames, doc moves) on Sonnet; keep design, review fixes and concurrency work on Opus.

# Review rounds

A change that moves state in steps gets the failure table (step, state moved, failure, recovery, test) in its `design.md` or PR body before the first review round.
Trigger: two findings of the same class in different rounds, or three blocking rounds in a row.
Then stop single-finding fixes and do one class pass: name the class and enumerate every instance from the code, not the findings.
Add each instance to the failure table; fix each row lacking a recovery or a test, fail-first.
Send the next round with the table; as integrator, name the class in the round brief and record the trigger in `sd task note`.
Split the PR if the table shows it does too much; a deferred advisory filed as its own item does not count, so cite it in later rounds.

# Tools

Run `codex exec` as `timeout N codex exec -s read-only -o <out> "<prompt>" < /dev/null > <log> 2>&1`: without `< /dev/null` it waits on stdin forever.
No new `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` within a minute means it hung: stop it and fix stdin.
Claude Code strips `CLAUDE_CODE_OAUTH_TOKEN` from Bash: source it on the line that starts `claude`: `. "$HOME/.config/shell/env.sh" >/dev/null 2>&1; <cmd>`.
Never copy that token into `settings.json`, a script or a log.

# Gen-AI experiments

Send every Gen-AI experiment's traces to `~/repos/system/local-genai-traces`: one collector keeps runs comparable; `genai-traces.sh endpoint` prints its endpoints.
Run `genai-traces.sh status` first and start the collector unless it exits 0.
Set `openinference.project.name` on a new source so Phoenix gives it its own project.
Run Aura experiments only through `mezmo-aura/aura.sh experiment start|run|inspect`: it runs the local `aura-local:instrumented` image, labelled with its source commit.
Rebuild that image with `aura.sh image` after changing the Aura checkout; `AURA_REPO` in `<config>/mezmo-aura/.env` names it.
Keep Aura changes local: never push the Aura checkout or open an upstream PR unasked.

# Generated HTML and UI

Never publish a Claude artifact unless the user asks for one by name.
Write generated HTML (reports, plans, review summaries) to `<repo>/docs/dashboard/`: it is gitignored, and the sd dashboard finds it on disk.
A repo without that folder gets one plus a `.gitignore` line.
Invoke the `hallmark` skill (`~/.agents/skills/hallmark`) before designing or changing a UI, a dashboard page or an HTML report.
Make every personal web design look made for its content, not generated from a template.

# Time

Report every time in the machine's local zone with its label, for example `09:36 MDT`; convert UTC (`Z`) first.
Keep raw timestamps unchanged inside quoted tool output and code blocks.
