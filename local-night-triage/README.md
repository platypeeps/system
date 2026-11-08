# local-night-triage

The night triage job (sd:8143). Each morning at 08:40, after the day's
scheduled jobs, one unattended Sonnet run answers one question: are there
system, workflow or operational issues on this machine? It repairs what it
may, files code bugs, and this machine's alerts get settled.

The design is `docs/work/2026-11-08-night-triage/design.md`.

## Usage

```sh
./night-triage.sh run      # the four steps below, once
./night-triage.sh test -v  # this folder's suite, with a fake sd and claude
```

Install it on every machine: copy `local-cron-jobs/examples/night-triage.job`
into `<config>/cron-jobs/jobs/`, list `night-triage` in the machine's
`.cron` profile, then run `cron-jobs.sh install night-triage`.

## Steps

1. **Release.** A claim an earlier run left in `state/claims` goes back to
   its old status while its last status change is still this job's claim;
   one another session claimed since is left to it. An `sd` failure keeps
   the claim in the file and stops the run with exit 1.
2. **Claim.** `sd reports list`; each `planning` alert whose report names
   this machine (lower-cased `hostname -s`) is claimed with
   `sd task status N in_progress --if-revision`. Its row goes into
   `state/claims` before the status change, so a crash between the two
   still leaves a row to reconcile. An alert already claimed is skipped,
   and so is one another session claims first.
3. **Triage.** `claude -p` on Sonnet reads the alerts, the Operations, this
   machine section of `sd today`, and local status. A failed `sd today`
   fails the run. Operator ruling "Repair
   and file": it may rerun a failed job, restart a service, clear a cron log
   file it proves stale, and file a code bug with `sd task add` in the owning
   repository (`--belongs-to` the system or pack checkout on a satellite).
   It holds no merge, push, sudo, scheduled-job, permission, mail, Slack or
   credential tool, and no alert writer. It replies with one answer per alert.
4. **Settle.** Each alert goes back to `planning` and gets one note:
   - `fixed` with evidence for every followup open at settle time (read
     again, so one a rerun added counts), each one the ingest's own:
     acknowledged with `sd reports acknowledge N --resolve-followups`;
   - `fixed`, but a person wrote an open followup: stays open for that
     person, since only they accept a fix or check of their followup;
   - `filed`: stays open and cites the item;
   - `operator`: stays open on Today, with the exact action and why no tool
     can do it;
   - anything else: stays open with a "not settled" note.

The raw reply stays as `state/<date>.reply`. `SD_PACK_ROOT` names the pack,
and `CLAUDE_BIN` names the claude binary.

## Failures

A failed step exits non-zero, and cron's failure channel reports it. A
failure before step 4 gives every claim back with no note. The failure table
is in the design.
