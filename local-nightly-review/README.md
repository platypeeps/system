# local-nightly-review

The nightly review job (sd:3638). Each morning one read-only Sonnet run reads
the past day's workflow, sessions and operations. It files up to five
suggestions as one review batch. The operator approves or rejects each one in
the dashboard, and the next run turns an approval into a task.

The design is `docs/work/2026-10-12-review-queue/design.md`, section "Nightly
review job". The queue is the pack's `sd-db review file|export|apply`; this
folder opens no database.

## Usage

```sh
./nightly-review.sh run      # the three steps below, once
./nightly-review.sh test -v  # this folder's suite
```

Install it as a cron job: copy `local-cron-jobs/examples/nightly-review.job`
into this machine's `<config>/cron-jobs/jobs/<host>/` on the hub, then run
`cron-jobs.sh install nightly-review`. It runs at 06:15 with a 30-minute limit.

## Steps

1. **Apply.** `sd-db review apply nightly` files one task for each Approve
   ruling, under the ref `review-approved:<note>`. It expires a suggestion that
   waited 7 days without a ruling, and closes each settled batch.
2. **Review.** This step runs only when today's batch file does not exist yet.
   It lists the subjects the queue rejected in the last 90 days, through
   `sd store items` and `sd-db review export`. Then `claude -p` runs on Sonnet
   with read-only tools and `prompt.md`, and replies with suggestions as JSON.
   `nightly_review.py batch` writes the reply as
   `<bulk root>/system/nightly-review/<date>.json`, through a `.part` file and a
   rename. The job sets every field but the questions: queue `nightly`, mode
   `open`, limit 5, and the options Approve and Reject.
   A malformed suggestion is dropped and named on stderr; the rest file. A reply
   whose every suggestion is malformed is refused. The raw reply stays as
   `<date>.reply` beside the batch file.
3. **File.** `nightly_review.py check` refuses a file that is not an open
   batch of the `nightly` queue for today, with a limit of 5 and the options
   Approve and Reject. The job files no blind or sealed batch: those need the
   dashboard's Scoring tab on Decide (lead ruling on sd:3635). Then `sd-db review file`
   files the first five suggestions in the model's order. It drops a subject
   rejected in the last 90 days and prints each drop.

The bulk root is `sd config get sd.bulk_storage_root`. `SD_PACK_ROOT` names the
pack, and `CLAUDE_BIN` names the claude binary.

## Failures

A failed step stops the run with a non-zero exit, and the cron job reports it.
A failed run is unknown, never "no suggestions". A valid reply with no
suggestions writes the file, files nothing and exits 0.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Settings | Nothing | Bulk root unset, or its volume not mounted | Exit 1 before the model | `test_no_bulk_storage_root_fails_before_the_model`, `test_an_unmounted_bulk_storage_root_fails_before_the_model` |
| Apply | Tasks and closes, in the pack | A pack without `sd-db review` | Exit non-zero before the model; nothing filed | `test_a_pack_without_review_fails_before_the_model` |
| Rejections | Nothing | `sd store items` or an export fails | Exit non-zero before the model | `test_a_failed_read_of_earlier_rejections_fails_before_the_model` |
| Review | `<date>.json` through `.part` and a rename | `claude -p` fails, times out, or replies with no batch | No file; exit non-zero; the rerun asks again | `test_a_failed_model_run_writes_no_file_files_nothing_and_fails`, `test_a_reply_that_is_not_a_batch_writes_no_file_and_fails` |
| Review | `<date>.reply` | One suggestion breaks the schema | That suggestion is dropped with a stderr line; the rest file | `test_one_malformed_suggestion_drops_itself_and_the_rest_file` |
| Review | `.part` only | The run stops before the rename | The rerun asks again and files once | `test_a_stop_after_the_reply_before_the_rename_runs_the_model_again` |
| File | The batch, in the pack | The run stops after the rename, before filing | The rerun files the file without the model | `test_a_stop_after_the_rename_before_filing_files_without_the_model` |
| File | Nothing | The job reruns the same day | No model run; filing the same file is a no-op | `test_a_second_run_the_same_day_runs_no_model_and_files_nothing_new` |
| File | Nothing | A blind, sealed, other-queue or other-limit file | Refused; exit 1; nothing filed | `test_the_job_files_no_blind_or_sealed_batch`, `test_a_batch_file_for_another_queue_or_limit_is_refused` |
| File | Nothing | The pack refuses the batch | Exit 1; the file stays for a look; the next day writes a new one | `test_a_batch_the_store_refuses_fails_and_files_nothing` |
