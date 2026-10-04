# Implement — planning documents get a trigger

Four pull requests here and two in the pack. The order is forced in one place
only: PR 2 cannot be *useful* until the pack's `--from sd:<id>` lands, but it
can be built and merged before it, because a planning run with no seeding is
the behaviour that exists today.

## PR 1 — the folder, the entrypoint, and `status`

`local-sd-plan/` with `sd-plan.sh` answering `help` and `status` and refusing
everything else, plus `README.md` and the participation conf.

- [x] `local-sd-plan/sd-plan.sh` — POSIX sh, `set -e`, `DIR="$(cd "$(dirname "$0")" && pwd)"`.
- [x] No-arg prints usage to stderr and exits 1; `-h|--help|help` exits 0.
- [x] `status` exits 0 configured, 3 not configured on this machine, 1 broken
      — convention 6. "Not configured" is: no participation conf. "Broken" is:
      conf present and the runner not dispatching.
- [x] `local-sd-plan/repos.personal.conf` — one repository path per line, `#`
      comments, and the file is **absent by default**; ship
      `repos.personal.conf.example` instead so participation is an explicit act.
- [x] `local-sd-plan/README.md`.
- [x] Tests. This folder tests a shell script, so it is Python against fixture
      trees for the same reason `local-repo-sync` is
      (`local-repo-sync/README.md:90`): the CI wrapper asserts `Ran N tests`
      and fails on skips, and a shell harness produces neither.
- [x] Wire the suite into `.github/workflows/system-native.yml`. **This is the
      step that gets forgotten** — `sd:404` is open against exactly this
      failure mode, "nothing notices a test suite that never gets wired into
      system-native". Adding a fifth suite is the change that makes `sd:404`
      concrete, so either wire it here or say in that row why not.

Verification: `sh local-sd-plan/sd-plan.sh status; echo $?` returns 3 on a
machine with no conf, and the new suite reports `Ran N tests` in CI.

Landed as #245 (2026-09-11), with the suite wired as `run_suite sd-plan`,
in `.github/workflows/system-native.yml`; the boxes above were ticked after
PRs 2–4 had already been, on 2026-09-12.

## PR 2 — `item <id>`, the executor

- [x] `item <id>` reads the row, refuses an item whose `repo` is not the
      enclosing checkout, refuses one that already has a `docs/work` row.
- [x] Derives the slug as `<created>-<title slugified>`, matching the three
      folders that exist. The cut is on a hyphen, never mid-word.
- [x] Invokes `claude -p "/sd-plan <slug> --from sd:<id> ..."
      --dangerously-skip-permissions`, the prompt ending (since 2026-09-17) in a sentence that says the run is unattended and names the three documents. The `--from sd:<id>` argument is not
      honoured until PR 6; passing it early is inert, not wrong.
- [x] Commits the three documents with a `Work: sd:<id>` line and pushes the
      branch itself. **The runner will not do this** — see `design.md` §3: the
      exec path skips the item transition entirely
      (`local-sd-db/sd_db/runner.py:333`) and the clone's `pre-push` hook
      refuses anything off the leased branch
      (`local-sd-runner/sd_runner/gitops.py:160`).
- [x] Registers the result, and **exits non-zero if the folder exists and the
      row does not**. Acceptance criterion 6 depends on this and nothing else.
- [x] Uses `local-sd-db/sd-db.sh work register` via the sibling source and not
      the pack's installed copy — that copy is taken at a tag and does not
      carry `register_work_item`. `register()`'s docstring says so, and names
      the runtime condition rather than a pull request: PR 5 has since landed
      and the call did not move, because the verb was never what was missing.
      It moved to the pack's verb with PR 5's last box, after sd:621.
- [x] Refuses before doing anything on: a dirty tree, a `done` or
      `ready_to_send` row, an unusable `created_at`. `--dry-run` writes nothing
      and reports what it would have done.

Verification: 14 tests in `tests/test_item.py` build a real checkout with a
real bare origin and a seeded database, and drive the whole path with a stub
agent — `sh local-sd-plan/sd-plan.sh test` runs them with the rest. What that
does not cover is a real `claude -p` invocation, which is deliberate: it is the
one part with no deterministic output, and PR 3's end-to-end run is where it
gets exercised for real.

## PR 3 — the palette entry, and the button

- [x] Register a catalog entry in `~/.local/share/sd/commands.yaml`:
      `plan-item`, `label: "Write the planning documents"`,
      `argv: ["<abs>/local-sd-plan/sd-plan.sh", "item", "{item}"]`,
      `screens: ["item"]`, `mutates: true`, `scope: "worktree"`,
      `placeholders: {item: item}`.
- [x] `commands.yaml` is machine state, not tracked here, so the tracked
      artefact is an example plus a `README.md` paragraph — the same split
      `local-sd-runner/commands.example.yaml` already uses. The entry went
      into that one example rather than a second file beside it: one catalog
      per machine wants one inventory.
- [x] Confirm the entry passes `runner_exec.catalog()` validation: absolute
      `argv[0]`, executable, uid-owned, not group/world-writable, basename not
      a shell.
- [x] **Give the row a branch before enqueueing.** The remedy already exists
      and did not have to be built: `sd runner prepare <id> --branch <name>`
      (`bin/sd_runner.py:77` in the pack) writes `repo` and `branch` on the
      row and touches git not at all. The button cannot do it on the way past
      — the dialog's path only enqueues — so it is a person's one-time step
      here and `nightly`'s first step in PR 4.

Verification, done: the entry is accepted by the live catalog with an
`executable_sha256`, and five deliberately malformed variants — a shell
`argv[0]`, a relative one, a non-executable one, an undeclared placeholder, an
unknown screen — are each dropped, so acceptance means something. Enqueuing
`plan-item` against item 439, whose `branch` is NULL, answers
`commands: item 439 needs a valid branch`, which confirms the refusal above at
the integration level rather than by reading `runner.py`.

Done, 2026-09-11 (this paragraph said "Not done" until 2026-09-17): the end-to-end run. `local.system-tools.sd-runner` is live and `dispatch_allowed` is true, so a successful enqueue dispatched at once: six `exec` assignments on sd:442, notes 686 to 718, 14:35Z to 16:30Z,
through the button. The fifth, exec note 709, wrote all three documents in 15 min 49 s and exited 1 at `register()`, on the registration path before #337; the runner's closeout pushed the branch anyway, and the folder merged as
`docs/work/2026-09-11-system-local-health-check-does-not-sweep-status/` in `2639a7aa` (#255), whose commit body calls it the first output of the sd-plan trigger, sent from the palette button onto a real row. The sixth, exec note 718 (16:30:01Z to 16:30:24Z, exit 0),
ended "already planned; nothing to do" and pushed nothing: a second trigger on the same row made no second folder, which is acceptance criterion 3's live evidence. (The 2026-09-17 delivery entry in `prd.md`'s Log does not accept it and leaves criterion 3 unticked; annotated 2026-10-03, sd:1238.) What remains is the owner's judgement of that output, or a second run on the
merged code, as the sd:438 note of 2026-09-17 lays out; either way the last acceptance criterion closes on a written judgement and nothing else.

**Silently dropped entries are their own finding.** A malformed entry does not
warn, it vanishes, and the only tell is a shorter catalog. Filed separately;
it is the same shape as the rule 6 manifest gap in `sd:440`.

## PR 4 — `nightly`, and the cron job

- [x] `nightly` enumerates `repos.registered()`, intersects with the
      participation conf, and for each takes the first candidate from
      `backlog_items(..., status='planning')` that has no folder.
- [x] One item per repository. The count is a single constant,
      `PER_REPOSITORY`, and a test pins that it appears once.
- [x] Sets a branch on the row with `configure_item` (what
      `sd runner prepare` calls) and only then enqueues through the same
      `prepare()` the button calls. A NULL branch is refused at enqueue for
      every role (`local-sd-db/sd_db/runner.py:53`).
- [x] Exits 0 when it selects nothing; exits non-zero only when it could not
      ask — no runner, no database, no readable palette. One repository
      refusing is named and skipped; a palette that cannot be read fails the
      night. The suite pins the difference, because collapsing them would
      turn "nothing is configured" into a silent successful no-op.
- [x] Checks `status` first and refuses to enqueue into a runner that is not
      dispatching, rather than filling a queue nobody drains. The entrypoint
      does it with the same helper `status` uses, and pipes the participation
      list to Python, so neither the conf nor the runner probe is parsed
      twice — that is how two answers drift apart.
- [x] `local-cron-jobs/jobs/sd-plan-nightly.job` with `JOB_COMMAND` (not
      `JOB_PROMPT` — selection is deterministic and the agent runs in the
      runner, not in cron). 04:45, clear of the 02:xx DarkWake slot.
- [x] Add the job name to `local-machine-setup/profiles/personal.cron`.
      A job file nothing names is installed by `install --every` and by
      nothing else.

Verification, done: `./cron-jobs.sh run sd-plan-nightly` on this machine —
which has no participation conf — exits 0, prints `no repository participates`
and enqueues nothing. `nightly --dry-run` against a conf naming this
repository and one path that is not registered names the unregistered one on
stderr and selects exactly one row. 15 tests in `tests/test_nightly.py` cover
selection, the refusals and the dry run; none of them enqueue, because on a
machine with a live runner an enqueue starts an agent.

## PR 5 (pack) — `sd work register`

Merged as `platypeeps/sd-ai-command-pack#816`. It was not "a parser and a
print": the row's branch had to come from `origin/HEAD` rather than from
whatever is checked out, because registration happens before the work branch
exists, and the guard against a library predating the verb turned out to be
load-bearing — CI's `sd_db` pin was two releases behind and every test met
the guard instead of the feature.

- [x] `bin/sd_work.py` gains a `register` subcommand calling
      `sd_db.workflow.register_work_item`. Planned as a parser and a print,
      on the assumption that the logic was already in the installed library.
      Delivered as a parser, a print, `docs_work.default_branch` reading the
      row's branch from `origin/HEAD`, and `_register_library`'s guard over
      the three modules the verb reads and writes through. Both additions are
      recorded above; neither was in the plan, and the second was the half
      that caught CI.
- [x] `sd-plan`'s SKILL.md gains the registration step, conditional on the
      work root's `.status-source` being `row`.
- [x] Then: PR 2's comment comes out and it calls `sd work register`.
      Blocked until pack sd:621 (`platypeeps/sd-ai-command-pack#887`,
      `9c789ad3`) made the verb write the working branch rather than
      `origin/main`; that commit is an ancestor of system CI's pack pin
      `e606446e`. Delivered as: `register()` runs `$SD_PACK_ROOT/bin/sd work
      register <path>` under `sys.executable`, the interpreter `sd-plan.sh`
      pinned; `sd-plan.sh item` no longer puts `local-sd-db` on
      `PYTHONPATH`; the module docstring and `register()`'s docstring are
      rewritten. Three decisions came with it. `SD_PACK_ROOT` is an
      environment variable defaulting to
      `~/repos/platypeeps/sd-ai-command-pack`, the path `local-bin-links`
      and `shadow-sync-nightly.job` already use. An absent pack is refused
      before the planning run, naming the path and the variable. A non-zero
      exit from `sd` is a refusal, quoting `sd exited N` and its stderr;
      `sd-plan` itself exits 1, as it does for a failed agent. Registration
      runs before the documents are committed, so a refusal leaves no
      commit: the runner archives a dirty clone instead of pushing it
      (`local-sd-runner/sd_runner/runtime.py:650-655`), and a retry
      registers the folder without planning again. Review of #337 found the
      first order (commit, then register) published the branch through the
      runner's cleanup and made the refusal permanent.
      One addition not in the plan: an interpreter without `sd_db` is now a
      sentence, not an `ImportError`, because no source tree stands behind
      it. `tests/test_item.py` pins the argv, the interpreter, `HOME`, the
      absent `PYTHONPATH`, both refusals and the sentence, and the
      end-to-end case runs the real verb at CI's pin and checks the row's
      branch is `plan/<slug>`.

## PR 6 (pack) — `--from sd:<id>`

Merged as `platypeeps/sd-ai-command-pack#815`. `sd:` is the third scheme and
the odd one: the database is not remote, so the citation carries no link — a
row is followed with `sd store item <id>`, and inventing a URL that resolves
nowhere would make the bullet look checkable when it is not.

- [x] `skills/sd-plan/SKILL.md` gains `--from sd:<id>` beside the existing
      `--from gh:` and `--from jira:` rows, seeding the interview from the
      row's `title`, `body` and `repo` and citing it under `## References`.
- [x] Then: PR 2's invocation gains `--from sd:<id>`. It always passed it
      (`local-sd-plan/sd_plan.py:247`); PR 2 shipped that argument inert on
      purpose. #815 is what makes it honoured, so this box closed without a
      line of change here — which is what "not honoured until PR 6" meant.

## Order and dependency

    PR 1 ── PR 2 ── PR 3        (the button works after PR 3)
              │
              └─── PR 4        (the job works after PR 4)

    PR 5, PR 6 (pack)  ──  independent; each improves PR 2 in place.

Nothing here blocks on the pack. A planning run without `--from sd:<id>` is a
planning run that interviews — which is what happens today.

## Closing the item

The last acceptance criterion is a person reading one unattended run's output
and judging it against what `/sd-plan` produces with a person answering. It
closes when that judgement is written down, and it does not close on a green
check. If the judgement is "not good enough", that is a finding about
`sd-plan`'s prompt and it belongs in a row of its own, not in this item.

## Log

- **2026-09-16** — The design page's two `path:line` citations into
  `local-cron-jobs/cron-jobs.sh` (lines 372 and 418, both mid-comment since
  the file grew above them) are repointed to form 1: line 14 cites
  `No overlapping runs of the same job` and line 81 cites
  `"$claude_bin" -p "$JOB_PROMPT"` in `cmd_exec`. `KNOWN_WRONG_OWNER` in
  `tests/test_citations.py` is empty. Nothing above is ticked: the remaining
  step is the owner's live run.
- **2026-09-17** — Three corrections before the owner's live run (sd:438's
  audit note of that date, rows 1, 3 and 5). The prompt `agent()` sends now names
  `prd.md`, `design.md` and `implement.md` and says the run is unattended,
  because the skill writes the second and third only when asked and
  `plan()` refuses the run without them; the one run so far passed only
  because the agent read the dispatcher's source. Both pages now cite the
  assignment budget of 90 minutes, not the runner's 24-hour subprocess
  timeout they cited before, and one run has been measured at 15 min 49 s (exec
  note 709 on sd:442). The PR 3 paragraph above that said the end-to-end
  run was not done now says it was, on 2026-09-11, and names its merged
  output. Every edited site keeps its line count where a ratchet key sits
  below it, and this line is appended.
- **2026-09-17** — Delivered: the owner's live run and judgement are on
  sd:438's note 2707 (line 8: good enough) and comment 2711 (nightly by
  hand, rc 0). The prd's Log names what is ticked and what stays open. The
  citation defect in sd-plan's prompt is sd:990. This line is appended.
- **2026-09-17** — The delivery line: #448 merged as 82ab7cfe on main with
  `Delivers: sd:438` in its message, but a CI re-run commit squashed in after
  it left the trailer outside the block git reads (the pattern sd_lib's
  `DELIVERS_TRAILER` notes). This commit carries the trailer where git reads it.
