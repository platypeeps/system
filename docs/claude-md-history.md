# CLAUDE.md history and rationale

This page keeps the reasons and incident stories that the root `CLAUDE.md` used to carry.
`CLAUDE.md` and `.claude/rules/` now hold rules only.
Each section below quotes the original text as it stood at `11b21c9`, before the split.
Read a rule in `CLAUDE.md` or `.claude/rules/`; read here why it exists.
Add new incidents and findings to this page, not to `CLAUDE.md`.

## Tests and CI: why suites are not counted

**Do not count them here.** `run_suite` lines in
`.github/workflows/system-native.yml` are the enumeration, and a number
written in prose is one more inventory that goes stale — this sentence said
"all four suites" while a fifth was being added. The `run_suite` list is
hand-maintained too, so the job's preflight enumerates `*/tests/test_*.py`
from the filesystem and fails naming any folder no `run_suite` line names:
a suite that exists but is not wired in no longer stays silently green.

## `sd-docs-lint`: the working directory and `--work-dir`

The working directory selects the repository to check — that is R10-D6, and
the flag it replaced used to make a run from inside a consumer check the pack
instead. `--work-dir` already defaults to `docs/work` and its own help calls
it repo-relative.

**Do not hand it an absolute `--work-dir`**, which is what this file used to
document. It looks harmless because every other rule accepts it — `repo /
<absolute>` is the absolute, so rules 1, 2 and 6 check exactly the right
items. Rule 7 does not take a path: it interpolates the string into a regex
and matches that against the prose of tracked markdown, where references are
written repo-relative. An absolute value cannot match one, so the rule reads
**zero** references, finds nothing, and prints a pass — the citation-drift
guard silently not running, with a green gate on top. Filed as
`platypeeps/sd-ai-command-pack#809`; until it lands, omitting the flag is the
fix.

## `sd-docs-lint` became a CI gate (#238, #230)

**Since #238 it is a gate and not only a habit.** `system-native` runs it on
every pull request and every push to `main`, from the repository root and
with no `--work-dir` — the same invocation printed above, so the local run and
the CI run read the same rules over the same items. It sits in the preflight
rather than behind `run_suite`, which asserts a unittest summary the lint does
not print, so `set -e` is what fails the job; and it runs before the suites,
because it takes under a second and they take minutes. The pack is pinned
by SHA in the workflow, so the rule set changes only when that pin moves.

Keep running it locally anyway: it answers in under a second, and the job
takes three minutes to tell you the same thing. The one rule that differs
between the two is rule 5, which needs a pull request body — CI supplies one
with `--pr-body` (see the `Work:` line under **GitHub for this repo**), and a
local run without one reports it not run. Why it went in: after #230 `main`
carried 165 rule 6 failures for five days with every check green, and what
found them was a human running the lint by hand.

## Work rows: why registration is manual

**A new folder needs a row, and nothing makes one for you.** Status lives in
the database here — `docs/work/.status-source` says `row` — and the import
that used to create the row now names each retired repository, exits 0 and
creates nothing (`_retired` in `local-sd-db/sd_db/jobs/cli.py`; #264) — and
every repository is retired. So a folder written today has no row and no
readable status until you run, from this repo's root:

    ./local-sd-db/sd-db.sh work register docs/work/<item>/prd.md

It takes the title and the date from the prd's frontmatter, the commit from
git (and the branch too, when the checkout is on one that is not the default;
otherwise `branch` stays NULL until `sd runner prepare --branch` sets it), and
files the item as `planning`. Running it twice reports
the row that exists. Skip it and `sd-status` reports the item as
`status-unreadable`, which is exactly what happened to
`2026-09-10-held-bumps-watch-their-blockers` and cost a hand-written `INSERT`
into the shared database to undo.

## Convention 2: the `adversarial-gate` symlink break (2026-09-03)

2. **POSIX sh** (`#!/bin/sh`, `set -e`), resolve the folder with
   `DIR="$(cd "$(dirname "$0")" && pwd)"` instead of hardcoding paths.
   **A script `local-bin-links` links is the exception**: `$0` is then the
   symlink in `~/bin/common`, so a script that reads files sitting next to it
   finds nothing. Walk `$0` to its real location first — the scripts that do
   carry the same `while [ -L "$SELF" ]` loop (`grep -l` for it; the count
   this sentence used to give was one short), each with a comment saying
   why. Adding a row to `LINKS` without
   this is how `adversarial-gate` shipped broken on 2026-09-03: it died with
   `missing core prompt: ~/bin/common/core.md`, and only through the
   link — direct invocation passed the whole time.

## Convention 6: why the sweep reads `help` output

6. **A `status` subcommand answers with an exit code, not just prose.** `0`
   healthy, `3` nothing to check (not configured on this machine, or not
   running), `1` up and actually broken. `local-health-check` runs the
   `status` of every tool that **declares** it and reports only `1`, so a
   machine that was never configured for a service stays silent instead of
   raising a nightly finding nobody can act on. A tool whose `status` cannot
   distinguish "unconfigured" from "broken" makes that check lie. Declaring
   is one sentence in the tool's `help`, next to its `status` line:
   "local-health-check reads these codes". The sweep probes every sibling
   folder's entrypoint for `help` and runs `status` on the ones whose output
   contains `local-health-check`; there is no list of tools in
   `health-check.sh`, so leaving the sentence out keeps a tool out and
   nothing else needs editing to bring one in. Do not name the tools that
   implement it here — the list this sentence used to be was one tool short
   the day it was checked. Ask the machine, and ask the sweep itself: the
   `status sweep: N tool(s) declared, M checked` note at the end of
   `local-health-check/health-check.sh check` is the count, and the findings
   above it name the tools. **`grep -l 'local-health-check' */*.sh` is not
   that question** and this file recommended it until
   `local-weekly-digest/weekly-digest.sh` grew a comment naming the sweep.
   That script has no `status` verb and declares nothing, and the grep listed
   it anyway: a file that mentions the sweep is not a file the sweep runs.
   The declaration lives in `help` **output**, which is why the sweep runs
   `help` rather than reading the file. Swapping the grep for a hand-rolled
   `for s in */*.sh; do sh "$s" help | grep -q ...` is not the substitute it
   looks like either, and for two reasons. `*/*.sh` is not the sweep's
   corpus: the sweep probes each folder's one entrypoint, and the glob also
   runs helpers and test scripts that no convention says answer `help`. And
   it hangs — `local-machine-setup/profile-autocapture.sh` does not answer
   inside eight seconds — which is why the sweep bounds every probe
   (`HELP_BOUND`) and raises a finding when one times out, rather than
   waiting.

## Jev: the reasons behind the contract

`local-jev` puts TypeSafe's Jev model on `PATH` as a command, so a judgment
that used to need an agent session can be a `JOB_COMMAND`, a hook, or a line
in a script. It is experimental, and the rule that comes with it is not a
style note: **every caller keeps the mechanism it had, and the machine runs
the same without Jev as with it.**

A caller proves that in one of two shapes, and a change that uses neither is
wrong:

    if jev enabled JEV_MY_STAGE; then verdict=$(jev noul ...); else verdict=$(old_way); fi
    verdict=$(jev choice ... --fallback "$(old_way)")

`jev enabled` exits 0 when Jev can answer on this machine and 3 when it
cannot, and it calls nothing to decide. `--fallback` prints the answer you
gave it and exits 0 when Jev is off, unkeyed or failing, with the reason on
stderr — loud, because a lane that quietly stops running is the defect this
repository has already been bitten by.

**A caller names its own stage in that call, and does not read its variable
itself.** `jev enabled JEV_MAIL_INTAKE` answers both halves at once: can Jev
answer here, and has that variable been used to switch this one stage off.
The words it accepts are the switch file's own — `0 off false no disabled`,
case-insensitively — and they live in `local-jev/jev.py` rather than in a copy
per caller. A caller that tests `[ "$JEV_X" = 1 ]` is the shape this replaced.

**Unset means on**, for a stage exactly as for the switch file. Every
integration was an opt-in at first, and a per-caller switch that defaults to
off makes each one added after it silently never run — the same failure the
kill switch was written to avoid, repeated once per caller. A stage variable
only ever subtracts: it cannot switch a stage on against a machine with no
key or with `jev off`.

**`jev off` is the kill switch**, and it is a file (`~/.config/jev/enabled`)
rather than only a variable, because cron and launchd read no shell profile.
`JEV_ENABLED=0` does it for one call. An absent file means enabled: a switch
that defaults to off makes every integration added after it silently never
run. **A machine with no `TYPESAFE_API_KEY` behaves exactly like one with the
switch off**, so a caller written against the switch is already correct on a
machine that was never keyed.

**A test suite that runs a caller end to end now has to switch its stage
off**, because unset reaches Jev. Several did not, and one of them spent real
tokens against the live endpoint before it was caught
(`local-health-check/tests/test_sweep.py`). The stubs those suites inject
answer `enabled STAGE` by reading that variable, the way the real command
does; a stub that ignores the argument cannot express "switched off" and
orders a run the caller had turned off.

**A caller of Jev also lives outside this repository, and it reads prose from
here.** `sd-docs-lint`, the pack's binary, takes a claim-support reading over
`docs/work` — and it picks its repository from the working directory, so the
run this file tells you to make from this root sends *this* repository's
citing sentences and cited passages to a third party. It is on by default
since `platypeeps/sd-ai-command-pack#1119`, and the pack documents the switch
in its own `CONTRIBUTING.md`, which nobody standing here opens. So it is here
too:

    JEV_SD_DOCS_LINT=0 <the sd-docs-lint invocation>   # or export it once

The invocations that take the reading **over this repository's prose** are the
bare lint above and `sd-ship` at delivery — the ones that run with this
checkout as the working directory. `make check` in the pack is not one of
them, and this paragraph said it was: that target runs the lint with the
*pack* as cwd, so it reads the pack's own `docs/work` and never this one's.
Which checkout the command runs in is the whole of the rule, and naming a
command by what it does rather than by where it runs is how the exception got
written down as an instance. What leaves the machine is capped and carries no
path, no item name and no citation marker — but it is prose from `docs/work`,
and some of it is not public.

What Jev is for here is ordering, routing and triage — which finding to read
first, which lane an item belongs in, whether a digest needs a human tonight.
It does not approve, merge, send, or delete. And nothing sensitive is piped
into it: every call leaves the machine, which is why `local-scan-for-secrets`
is not a caller even though triaging its hits is the textbook use.

## Ports: why the list defers to the machine

- No two `local-*` services collide any more; all 13 can run at once. The
  same-role pairs were split by moving the non-canonical holder: falkordb to
  **6380** and graphiti's bundled falkordb to **6381** (redis keeps 6379),
  graphiti's UI to **3004** (falkordb keeps 3003), jaeger's OTLP to
  **4327/4328** (the collector keeps 4317/4318, receiving OTLP being its whole
  job), and the clickhouse MCP to **8002** (redisinsight keeps 8001).
- Remaining overlaps are all with software outside this repo: `3000`
  (grafana/worldmonitor dev server), `8080-8083` (jaeger HotROD vs otel-demo
  port-forward on 8080 and the host google_workspace_mcp on 8083), `8084`
  (llama-cpp, overridable via PORT), `8766` (task-actions, overridable via
  TASK_ACTIONS_PORT).
  graphiti's MCP HTTP sits on `8085` to dodge 8083/8084.
  `8767` is the workflow dashboard's loopback port, next to task-actions on
  8766. It is served by `local-project-dashboard/dashboard.sh serve --port`.
  Its explicit configuration must use the same backend port. Private Tailscale
  access uses HTTPS on 8443. Optional IP access binds the node’s Tailscale
  address on 8768 and authenticates its TCP peer. Both leave public 443 alone.
- Two services moved off their conventional ports because non-repo software
  already owns them: `local-postgres` is on **5434** (other projects'
  containers hold 5432 and 5433) and `local-qdrant` on **6337** (OpenWhispr
  bundles a qdrant on 6333/6334, Miyo one on 6335/6336). Both are overridable.
- **Do not trust this list — ask the machine.**
  `local-machine-setup/machine-setup.sh candidates service` reads each
  service's effective ports out of the scripts (honouring exported overrides),
  prints `CLASH` for two services wanting one port and `BUSY` for a port
  something already holds. This list is prose and goes stale; that command
  cannot. It is what caught 5433 being taken after 5432.

## `local-research-kit` moved to the pack (2026-09-01 to 2026-09-03)

- **`local-research-kit` is gone as of 2026-09-03 — it lives in the pack now.**
  Six research repos depend on it, and what they depend on moved to
  `platypeeps/sd-ai-command-pack`: the standard is
  `skills/sd-research-repo/references/conventions.md`, the tooling is
  `bin/sd-research-kit`, and `tokens.css` is inlined in
  `bin/sd_research_tokens.py` because the pack ships no shell outside its own
  tooling and renders `skills/` as markdown only — that is what
  `tests/test_no_shipped_shell.py` checks; a `.css` in `bin/` would pass it,
  so "Python only" there is convention, not a test.
  It went there rather than staying split because the standard and the code that
  enforces it drift the moment they are versioned in two repositories — the same
  reason it left its original standalone checkout for here on 2026-08-27.
  What is left here is `local-bin-links` putting `sd-research-kit` on `PATH`
  from `$SD_PACK_ROOT` (default `~/repos/platypeeps/sd-ai-command-pack`) — with
  every other `bin/sd*` command the pack ships, read from its `bin/` on each run
  rather than listed — because PATH wiring is machine setup and the pack's
  installer does not do it.
  A missing pack checkout is a `SKIP`, not a failure.
  Dropping the old `research-kit` row could not remove the symlink it had
  already made — a row that is gone names nothing to sweep — so `bin-links.sh`
  grew a `RETIRED` list that `install` and `remove` clear and `status` reports.
  Empty it once every machine has run `install` past this change.
  **The dependency also runs the other way now, and that is the surprising
  half.** The pack is public and its `sd-research-repo` skill tells its readers
  to run `adversarial-gate render --lens research-brief` — a command that lives
  *here*, in a private repo, as `local-adversarial-gate`. So this repo owes the
  pack a second `local-bin-links` row, added 2026-09-03 (#208) after the move
  shipped an instruction nothing on any machine could execute. When a document
  here moves to the pack, check what it tells the reader to *run*, not only what
  it says: a path that leaves with the prose becomes a command that has to be on
  `PATH` on every machine that reads it.
  Two things changed for callers: the command is **`sd-research-kit`**, not
  `research-kit`, and **no verb takes a repo path** any more — run it from
  inside the repo. R10-D6 in the pack forbids a command that can be pointed at a
  checkout the caller is not standing in.
  The six repos' own `CLAUDE.md` was updated in the same change, on a
  `chore/research-kit-moves-to-pack` branch in each — they now point at the
  standard with the command (`sd-research-kit conventions`) instead of a path, so
  a future move inside the pack does not have to be chased into six
  repositories. That is the rule here: this kit's blast radius is six other
  checkouts, so grep them, not just this repo.
  The kit's `IMPROVEMENTS.md` did **not** follow it into the pack — the pack is a
  public repo and that file names internal repositories and a specific
  unapproved-source incident. It is `RESEARCH-BACKLOG.md` at this root, which is
  why this repo has a root markdown file that is not a tool.
  Five of the six moved on 2026-09-01 into one `~/repos/research/` folder, one
  subfolder per research repo. Discovery elsewhere survives it — `repo-sync.sh` globs
  `$ROOT/*/.git` *and* `$ROOT/*/*/.git`, and the dependabot routine's
  `find -mindepth 2 -maxdepth 3` still reaches a checkout one level deeper.
  `local-project-dashboard` vendored its `tokens.css` from the kit until
  2026-09-01, when the page it styled was deleted. The current workflow UI uses
  `local-project-dashboard/sd_dashboard/static/dashboard.css`.

## Deliberate convention deviations

- Deliberate convention deviations: `local-scan-for-secrets` scans the cwd on
  no-arg (its natural default) instead of exiting 1; `local-cswap` is bash,
  not POSIX sh (arrays/pipefail); `local-gito` sources `~/.gito/.env` (its
  upstream convention) rather than a folder-local `.env`; `local-cron-jobs`
  uses the `local` keyword (not strict POSIX, but supported by every shell
  that runs it — a rewrite would cost more than it buys).

## Replacing a daemon is not restarting it

**Replacing a daemon is not restarting it.** Config held inside a daemon's own
state — a tailscale serve config, anything under `/Library/<vendor>` — survives
reboots and so reads as permanent, then vanishes the first time the package
manager behind it changes. When a migration swaps who provides a service,
enumerate what that service was *holding*, not just whether it comes back up.
After any such swap on a profile carrying the `task-actions` launchd job, check
`tailscale funnel status`: the server behind the funnel stays fine while the
public URL is gone, so every status line reads green.

## The 02:xx cron slot

`repo-sync-nightly` runs at 02:45, and a laptop in deep idle answers that slot
inside a DarkWake maintenance window where networking is maintenance-only. The
job runs, finishes, and fails every repo — split between `connect to host
ssh.github.com port 443` and `Permission denied (publickey)`. `pmset -g log`
names the wake; the later slots (03:15, 03:45, 04:15, 04:30) are unaffected, so
this is a slot problem and not a night problem.

- **The on-disk key cannot cover for the agent.** The on-disk SSH key is
  passphrase-protected, so every unattended git call depends on `SSH_AUTH_SOCK`
  in the launchd domain.
- **The remedy is a scheduled wake, not a cron edit.** `JOB_SCHEDULE` is
  fleet-wide and no night hour is reliably awake on a laptop, so moving the job
  an hour is guesswork dressed as a fix.
- **The wake is a per-machine system setting and no stage here sets it**:
  `sudo pmset repeat wakeorpoweron MTWRFSU 02:40:00`. Before trusting a green
  night on any machine, check `pmset -g sched` for a `Repeating power events:`
  block.
- **A green night is not evidence the wake exists.** An awake machine passes
  this slot on its own, which is exactly the kind of green that hides the bug.
  `repo-sync.sh nightly` exits 1 when half or more of the fleet fails, so the
  failure itself is loud.

## Three sessions: the 2026-09-10 checkout incident

`~/repos/system` is a primary checkout more than one session reaches into:
this repo's own sessions, the pack's (it renders `local-statusline` from
here), and the operator by hand. A primary checkout has exactly one owner —
the session that opened it. Worktrees belong to whoever created them.

- **Do not move a HEAD you did not open.** No `checkout`, `switch`, `pull`,
  `reset` or `stash` in a checkout another session is working in. Reading is
  always fine: `git show`, `git log`, `git diff`, and reading a file at a ref
  move nothing, and every question about `main` can be asked that way from a
  branch.
- **Post-merge verification runs in a worktree of the merged commit**, never
  by putting the primary checkout on `main` — `git worktree add
  "$SCRATCHPAD/verify" <sha>`, check it, `git worktree remove`. The owning
  session returning its own checkout to `main` after its own branch merged is
  the ordinary end of its own merge, and is not what this forbids.
- **A dirty-file count is not a permission check.** On 2026-09-10 at 19:00:46
  a pack session ran `git status --short | wc -l && git checkout -q main &&
  git pull -q --ff-only origin main` here, thirteen seconds after
  squash-merging #232, to render a status line from `main`. It counted the
  dirty files and switched anyway, off the branch another session was working
  on. Nothing was lost, because that session had pushed — but this checkout
  held two of the operator's uncommitted files for hours earlier the same day,
  and a branch switch against uncommitted work either refuses or carries the
  edits onto the new branch.
- **The reflog cannot tell you it happened.** Every entry here is
  the same operator identity, and nothing records which session
  moved a HEAD, so a foreign `checkout: moving from <branch> to main` is shaped
  exactly like the owner's own. That is why this is a rule and not a check.

## Three sessions: a stale worktree is not an unlanded one (2026-09-20)

On 2026-09-20 a session compared four worktrees with `origin/main` file by
file, read every difference as work missing from `main`, and reported three
of them as unsaved work to rescue. All three were already on `main`
(`system-prompt` and `system-ship` via #451, `wt-health` via #470). The files
differed because `main` had moved 46 and 18 commits further on (sd:1184).

A superseded branch is a subset of `main`; an unlanded one is not. A symbol
comparison finds candidates, but it does not prove supersession:

    git show origin/main:<file> | grep -o 'def test_[a-z_]*' | sort > /tmp/main
    grep -o 'def test_[a-z_]*' <worktree>/<file> | sort > /tmp/wt
    comm -23 /tmp/wt /tmp/main    # a name here is certainly missing from main

An empty result proves nothing. A fix inside an existing function, or new
assertions under an existing test name, keeps every name and still has not
landed (review of #590, 2026-09-26). For each candidate, read
`git diff origin/main...<branch>` and check each hunk against `main`'s
current file. `git cherry origin/main <branch>` marks a patch already applied
with `-`, but only for a patch that landed unchanged. Keep work that stays
uncertain.

`git rebase origin/main` skips a patch already applied, with the same limit.
Under squash merges, `merge-base --is-ancestor` and unpushed-commit counts
both over-report unmerged work, and a worktree with no commits ahead can
still hold content that landed from elsewhere.

## GitHub: the `Work:` line and the token

- **A pull request says which row it advances on a `Work:` line.** One line,
  at the start of a line, naming either a `docs/work` item by path or a
  database row as `sd:<positive integer>` — `Work: sd:402`. `Refs: item #402`
  is prose; a `Work:` line is checked. CI hands the body to `sd-docs-lint`
  (rule 5) on every pull request, which fails a second `Work:` line and a
  malformed id, so `sd:0402` and `sd:+402` do not pass. A body with no
  `Work:` line is a note and not a failure: the rule is there for the claim
  being wrong, not for the claim being absent. What it checks is spelling.
  Nothing checks that the row exists or is yours: `sd-ship` writes the line
  for the item it is shipping, and a hand-typed id is verified nowhere.

- **`GITHUB_PERSONAL_ACCESS_TOKEN` is a classic PAT with `repo` scope**, in
  `~/.config/shell/env.sh`. Fine-grained PATs are per-org and per-repo, which is
  unworkable across several orgs, and under one the `mcp__github__*` tools 404
  on every private repo. With the classic token the global "MCP before `gh`"
  rule applies here.
- **An env change does not reach a running session.** The MCP server
  authenticates with `Authorization: Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}`,
  interpolated from the Claude Code process env inherited at launch. After
  rotating or replacing the token, restart Claude Code from a fresh shell —
  `/mcp` reconnect reuses the same stale env. Tell-tale: MCP 404s while `curl`
  with the same token returns 200.

## Slack digest: what was tried

- **The Slack plugin MCP works headless. Do not rebuild it.** The tool schema
  loads and a real search returns private-channel data from a bare shell under
  `claude -p --dangerously-skip-permissions`; `./cron-jobs.sh run
  slack-daily-digest` exits 0 and mails the digest. Stored memory claiming
  otherwise measured a dead OAuth token and read an auth failure as a
  headless-loading failure.
- **Do not build a local `korotovsky/slack-mcp-server`.** It was only ever the
  fallback for the token-refresh question, and the plugin's OAuth token
  refreshes unattended, so no fallback server is needed.
- **The job lives in `personal.cron`; `work.cron` is comment-only.** Moving it
  to `work.cron` on the "connector only the work machine has" theory was tried
  and reverted.
- **Mention search must query the user's handle (`<handle>`), not the
  `<@USERID>` form.** Slack stores a mention as `<@USERID|handle>`, so the
  bracket-closed form matches nothing and the digest silently reports 0
  mentions forever. A bare first name is wrong too — it false-positives on
  prose. The working form
  is in the vault SKILL.md.
