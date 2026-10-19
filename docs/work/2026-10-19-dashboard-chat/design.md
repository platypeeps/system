---
title: The dashboard's Chat pane sends a question to one bounded, read-only claude -p and shows the answer
created: 2026-10-19
item: sd:5294
---

# Design: dashboard-chat

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.

## Problem

Every dashboard page has a Chat tab in its right pane (`v2/static/shell.js`, `send()`), but nothing answers it:
`send()` prints "No chat backend is connected to this dashboard yet". The operator chose to build it.

## Approach

**One request, one run.** The pane posts `POST /api/chat` with `{scope, page, selected, text}` and waits for the answer.
The server runs one `claude -p` and answers `{answer, where}`, or an error that names why there is no answer.
No stream, no session, no history: each question stands alone, with the page and the selected row as its context.

**The run.** It reuses `email_draft`'s launch line and runner, not a copy of them:

- `email_draft.command(tools)` builds the `sh -c` line `email_draft.CLAUDE` was: it sources `~/.config/shell/env.sh`
  on the same line for `CLAUDE_CODE_OAUTH_TOKEN` (launchd gives the dashboard only the plist environment), raises the
  open-file limit, and finds `claude` in `~/.local/bin` when `PATH` lacks it. `CLAUDE` becomes `command('""')`,
  byte for byte the line it was.
- `email_draft._model(text, line, cwd=...)` runs it: the prompt on stdin, never in argv or the shell line; its own
  process group, killed whole past `MODEL_TIMEOUT` (120 s, the bound the proxy already carries for Draft email).
  After the kill it reads the pipes for at most `REAP_TIMEOUT` (5 s), then closes them, so a descendant that left
  the group cannot hold the request or the lock.
- Chat's line is `command("Read,Grep,Glob", bound=130)`: `exec perl -e 'alarm shift; exec @ARGV' 130 claude -p
  --model sonnet --restricted --strict-mcp-config --tools Read,Grep,Glob --no-session-persistence`. The alarm
  survives the exec, so the run ends 10 s after the runner's bound even when no dashboard process is left to kill it.

**Why these tools.** `--tools Read,Grep,Glob` is the whole built-in tool set the run has: no Bash, no Edit or Write, no
WebFetch, no Task. `--restricted` ignores the user, project and local settings files (so no hook or permission rule
from them applies) and confines the file tools to the working directory. `--strict-mcp-config` with no
`--mcp-config` loads no MCP server. The run therefore reads files in one checkout and writes, sends and merges
nothing. A tool-less run (`--tools ""`) was the alternative: it cannot look at the code a question is about, so it
answers only from the context the server pastes in. The read tools buy that at no write risk.
Read-only `sd` commands through `Bash(sd task show:*)` were rejected: a Bash allowlist is a pattern over a shell
line, and the run gains nothing the server cannot read itself. The server reads the selected item instead.

**Context.** stdin holds the fixed `PROMPT`, then the page name, the scope, the working directory, the selected row
(its type, label and id, as the page put it), and, when that row has an item, the item as the adversarial review reads
it (`adversarial._item_text`), cut to `ITEM_MAX` characters. The question comes last.

**Working directory.** The first that applies:

1. The selected row's item names a repo: that checkout, or no answer (503) when its folder is not on disk. Another
   repo's code would answer the wrong question.
2. Scope `pack`: the command pack checkout (`SD_PACK_ROOT`, as `sd_dashboard.pack_library` reads it).
3. Scope `hoa`: the registered `hoa` repo (`hoa_screen.checkout`), when it is on disk.
4. Anything else, the fleet scope included: this system checkout (`Path(__file__).parents[2]`, as `fix_session`).

**One at a time.** A module lock taken without waiting: a second question while one runs answers 409 "a chat answer
is still running". The lock lives in the server process only; a restart frees it.

**Auth.** `/api/chat` is one more branch of `server.action_route`, so it passes the same preflight as every dashboard
POST: same origin, `Sec-Fetch-Site`, the session cookie and the `X-SD-CSRF` token, a bounded JSON body. The action
writes nothing to the workflow database.

**Answers.** 200 `{answer, where}`; 400 for a malformed body; 404 for a selected item that does not exist; 409 while
another run holds the lock; 504 when `claude -p` gave no answer within the bound; 503 when it failed, answered
nothing, or the working directory is gone. Each error text says no answer was given. The answer is cut to
`ANSWER_MAX` characters.

**Front end.** `send()` posts through `shell.post`, shows a pending message, then swaps in the answer, or the named
failure. The row is taken when the question is sent, so an answer that arrives after `j` / `k` is kept with the row it
is about. Send is off while a question runs. The mockup (no `window.SHELL_PAGES`) keeps a sample reply. The post
and its failure text live in a `chat:start` block that the shell suite runs; the change stays inside `send()` and
its helpers, since another builder is editing `shell.js`.

## Non-goals

- No conversation memory: no `--resume`, no thread sent back. Each question is answered alone.
- No streaming. The 120 s bound and one JSON answer keep the request shape every other action uses.
- No writes, proposals or approvals from chat. The pane's "writes need your approval" line becomes "reads only".
- No queue: a second question while one runs is refused, not held.
- No new config key. Model, bound and tools are constants beside `email_draft`'s.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1. Check the body | nothing | unknown key, empty or long text, bad selected row | 400, no run, lock not taken | `test_chat.TheRequest.test_a_bad_body_is_refused_and_runs_nothing` |
| 1a. Preflight | nothing | no CSRF token, wrong origin | 403, no run | `test_chat.TheRequest.test_a_post_without_the_csrf_token_runs_nothing` |
| 2. Take the lock | in-process lock held | another run holds it | 409, the held run is untouched | `test_chat.OneAtATime.test_a_second_question_while_one_runs_is_refused` |
| 3. Read the item, pick the directory | lock held | item missing; directory gone | lock released in `finally`; 404 or 503 | `test_chat.TheContext.test_a_missing_item_answers_404_and_frees_the_lock` |
| 4. Run `claude -p` | a child process group | no answer in 120 s | the whole group is killed; 504; lock released | `test_chat.OneAtATime.test_a_timeout_kills_the_run_and_frees_the_lock` |
| 4. Run `claude -p` | a child process group | a descendant left the group and keeps the pipes | pipes closed after `REAP_TIMEOUT`; 504 | `test_email_draft.TheFailures.test_a_descendant_that_left_the_group_does_not_hold_the_timeout` |
| 4. Run `claude -p` | a child process group | non-zero exit, empty reply, no binary | 503 naming the last line; lock released | `test_chat.TheFailures` |
| 4. Run `claude -p` | a child process group | the dashboard process dies mid-run | the lock dies with the process; the run's own alarm ends it 10 s past the bound; it wrote nothing | `test_chat.OneAtATime.test_the_run_ends_by_its_own_alarm_when_no_dashboard_waits` |
| 5. Show | the pane's thread and Asked here | the operator moved to another row | the answer is kept with the row it was asked about | `test_v2_shell_shared.TheChat` (payload); the row capture is in `send()` |
| 5. Answer | nothing | the browser left | the run finishes or times out; the answer is dropped | none needed: nothing was written |

## Acceptance criteria

- [ ] `make test SUITE=dashboard` passes, with `tests/test_chat.py` failing on `origin/main` (404 "No such action").
- [ ] The run's argv is exactly `-p --model sonnet --restricted --strict-mcp-config --tools Read,Grep,Glob
  --no-session-persistence`, and `test_email_draft`'s argv pin still passes unchanged.
- [ ] A question with `$(touch pwned)` runs no shell; the workflow database is byte-identical before and after.

## Risks

- **Orphaned run.** The run has its own process group, so a dashboard restart (a new build, `CodeChanged`) leaves a
  run in flight. Its `perl` alarm ends it 10 s past the bound. The new process may start a second run beside it for
  those seconds; accepted, both only read. The alarm stops claude itself; a Grep child it started ends on its closed
  pipe. `email_draft` keeps the risk without the alarm, since its line is unchanged.
- **Trusted launch paths.** `~/.config/shell/env.sh` runs before claude's restrictions apply, and managed settings
  (with any hook they define) still apply under `--restricted`. Both are the operator's own files, as for Draft email.
- **Reads beyond what the page shows.** Read and Grep reach any file in the working directory, ignored files
  included. The answer goes only to the signed-in operator's own page. Accepted under the repository's threat model.
- **Prompt injection from a read file.** A file the run reads can steer the answer text. It cannot steer an action:
  the run has no tool that writes, runs or sends. Accepted.
- **Evidence.** The answer is tied to its question by the one synchronous request: no run file, no id to mix up. The
  item context is read by the server at request time, not taken from the page.

## Review

One read-only adversarial `codex exec` review of this design ran before the code (2026-10-19). It found:

1. Blocking: after the kill, `child.communicate()` had no bound, so a descendant outside the group could hold the
   lock. Fixed: `REAP_TIMEOUT`, with a test whose double leaves the group.
2. Blocking: a restart removed the only timeout. Fixed: the `perl` alarm in chat's line, with a test that runs the
   line with no runner watching.
3. Blocking: an answer that arrived after the operator moved rows was kept with the new row. Fixed: `send()` takes
   the row when it posts.
4. Blocking: an item whose repo is gone fell through to another checkout. Fixed: 503, with a test.
5. Advisory: `env.sh` and managed settings run before or beside the restrictions. Named under Risks.
