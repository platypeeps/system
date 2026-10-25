# local-home-prep

The home prep job (sd:6029). Each evening one bounded Sonnet run preps the home
items due tomorrow: a Gmail draft, a private Google Doc, or a checklist. The job
writes one `Prepped for <due>:` note per item with the drafts and links. The
operator sends or does it; the model holds no send or share tool (grill Q54).

## Usage

```sh
./home-prep.sh preview   # the items `run` would prep; no model, no note
./home-prep.sh run       # the steps below, once
./home-prep.sh test -v   # this folder's suite
```

Copy `.env.example` to `<config>/home-prep/.env` and set the account and the
repos. Run `preview`, then one `run` by hand, and read the notes. Then copy
`local-cron-jobs/examples/home-prep-daily.job` into this machine's
`<config>/cron-jobs/jobs/<host>/` on the hub and run
`cron-jobs.sh install home-prep-daily`. Nothing installs it before that.

## Steps

1. **Select.** `sd store items --open --json` lists the open items. An item
   counts when it is due tomorrow (local date), is a task, followup or
   personal item, is not parked, and has no repo or a repo in
   `HOME_PREP_REPOS`. An item with a `Prepped for <tomorrow>:` note already is
   skipped, so a rerun preps only the rest. A note for an earlier due date of a
   recurring item does not count.
2. **Prep.** `claude -p` on Sonnet, `--max-turns 60`, bounded at 30 minutes.
   `--restricted` drops the settings files, `--strict-mcp-config` keeps only
   the workspace MCP server, and `--permission-mode dontAsk` denies every tool
   `--allowedTools` does not name. It may read the items and their repos, read
   Gmail and Drive, make Gmail drafts and make a Google Doc. Send, label,
   share and permission tools are also named in `--disallowedTools`.
3. **Apply.** `home_prep.py apply` judges the run by the result object
   `--output-format json` prints, not by the exit code. Then it writes one note
   per item the reply names. The job never moves an item's status.

`SD_PACK_ROOT` names the pack, `CLAUDE_BIN` the claude binary, and
`WORKSPACE_MCP_URL` the MCP server (default `http://127.0.0.1:8083/mcp`).

## Failures

A failed step stops the run with a non-zero exit, and the cron job reports it.
A failed read is unknown, never "nothing due".

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Config | Nothing | `HOME_PREP_ACCOUNT` unset | Exit 1 before any read or model, both remedies named | `test_a_missing_account_fails_before_the_model` |
| Select | Nothing | `sd store items` fails | Exit non-zero before the model | `test_a_failed_item_read_fails_before_the_model` |
| Select | Nothing | An item's notes cannot be read | Exit non-zero before the model | `test_a_failed_note_read_fails_before_the_model` |
| Select | Nothing | Nothing due, or all prepped | Exit 0, no model run | `test_nothing_due_runs_no_model` |
| Prep | Gmail drafts, Drive docs | `claude -p` fails or times out | No note; exit non-zero; the rerun asks again, and the prompt has the model reuse a draft it made | `test_a_failed_model_run_writes_no_note_and_fails` |
| Apply | Nothing | Exit 0 with an error subtype, `is_error`, no result object, or no JSON reply | No note; exit 1 | `test_the_result_object_judges_the_run_not_the_exit_code` |
| Apply | Notes | The model was denied a tool | The notes of a valid reply are written, so the drafts are found; then exit 1 naming the tool | `test_a_permission_denial_fails_after_the_notes` |
| Apply | Some notes | The run stops after some notes | The rerun skips the noted items and asks only for the rest | `test_a_rerun_after_some_notes_preps_only_the_rest` |
| Apply | Nothing | The reply names an item not selected | No note for it; a stderr line names it | `test_a_reply_naming_an_unselected_item_writes_no_note_for_it` |
| Apply | The other notes | A selected item has no entry in the reply | The rest are noted; exit 1 naming it | `test_an_item_missing_from_the_reply_fails_the_run` |
