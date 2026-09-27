# local-obsidian-tasks

Nightly digest of due/overdue Obsidian tasks, emailed as HTML with three
buttons per task: **Open in Obsidian** (`obsidian://` link), **Mark done**
and **Postpone 3 days** (signed links served by `local-task-actions`).

## Usage

```sh
./obsidian-tasks.sh list   # print due/overdue tasks, no email
./obsidian-tasks.sh run    # print, and email the digest when non-empty
```

Installed as the `obsidian-tasks-nightly` job in `local-cron-jobs` (03:00,
listed in machine-setup's `common.cron`).

## How it decides

Reads the TaskNotes plugin's per-task files (`TaskNotes/Tasks/*.md`
frontmatter): a task is included when `status: open` and `scheduled:` is
today or earlier. Recurring tasks need no special handling — TaskNotes
advances `scheduled` to the next occurrence when an instance completes.

## Email

Goes through `local-notify`'s `email` channel with `-f html` (Gmail via the
local `google_workspace_mcp` server). No due tasks = no email. The job exits
1 only when the email could not be delivered, which triggers the cron
failure push, so a digest never vanishes silently.

All three buttons go through `local-task-actions`, fronted by its
Tailscale Funnel URL, so they work from any device; links expire after 7
days. Open is an https redirect to the `obsidian://` URI (mail clients
strip custom-scheme links), and done/postpone show a one-tap confirmation
page so mail-scanner prefetches can't mutate tasks. When task-actions is
missing the digest degrades to raw `obsidian://` Open links only.

## Ordering, and the optional Jev ranking

The digest is ordered most-overdue-first, which is not the same as
most-important-first: a three-week-old "water the plants" outranks a bill
due today. The digest asks Jev to score the tasks by urgency and prints them
in that order, so the first card in the mail is the one that matters.

It is **on by default and additive**, and one call decides it:
`local-jev/jev.sh enabled JEV_OBSIDIAN_TASKS` exits 0 when a key is present,
the fleet switch is on, and this stage has not been switched off. With any of
those missing the digest is the byte-for-byte digest it was before this
existed.

**Unset means on.** `0`, `off`, `false`, `no` and `disabled` switch this stage
off, in any case; every other value leaves it on, the `1` this used to require
included. It only ever subtracts: `jev enabled` reads the fleet switch and the
key first, so this variable can take the stage out and can never put it back in
on a machine that cannot answer.
 Jev is experimental, so
nothing here depends on it: a switched-off, unkeyed, failing or half-
answering Jev falls back to today's order, writes the reason to stderr, and
leaves the exit code alone. A run that was ranked says so, in one line of
the text body and one sentence in the mail's footer.

It never changes **which** tasks appear, never touches the buttons, their
URLs or their signatures, and never writes to the vault. `jev.sh` is called
by path from this folder (`../local-jev/jev.sh`), not through `PATH`, because
cron and launchd have neither this repo nor `~/bin/common` on theirs.

All tasks go out as questions in **one** `jev ask` request, which is how
they run in parallel; one request per task would pay a round trip per line
of the mail.

### What leaves the machine

Every Jev call leaves this machine, so the request carries the smallest span
that can answer "which of these is most urgent":

- each task's **title** (the frontmatter `title`, or the file stem)
- its **`scheduled` date** and **how many days overdue** it is
- today's date

Nothing else: no note body, no file path, no vault name, no
`obsidian://` link, no priority, no action URL. `tests/` asserts that on the
exact payload the tool hands to `jev.sh`.

## Tests

```sh
./obsidian-tasks.sh test
```

Runs offline against a fake repo root: a copy of the entrypoint with stubbed
`local-jev`, `local-notify` and `local-task-actions` siblings and a fixture
vault. Nothing reaches the network or `~/Documents`. The golden files in
`tests/fixtures/` are the digest this tool produced before Jev existed, with
the dates templated so they do not go stale; three of the tests assert the
mail is still byte-for-byte that. The harness passes its own date to the
script in `OBSIDIAN_TASKS_TODAY`, so a run that crosses midnight cannot count
"overdue" from a different day than the fixture was written on.

## Environment

- `OBSIDIAN_VAULT` — vault path (default `~/Documents/Obsidian Vault`; exported, or set in the gitignored `./.env`, see `.env.example`)
- `OBSIDIAN_TASKS_SUBDIR` — task folder (default `TaskNotes/Tasks`)
- `JEV_OBSIDIAN_TASKS` — `0`, `off`, `false`, `no` or `disabled` switches the
  Jev ordering off; unset means on
- `OBSIDIAN_TASKS_TODAY` — the `YYYY-MM-DD` due and overdue count from
  (default today); the test suite pins it
