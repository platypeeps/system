---
title: A daily night triage job settles each machine's own alerts
created: 2026-11-08
item: sd:8143
---

# Design — night-triage

Write this page only for a change that moves state in steps.
The item's row holds status, progress and decisions; this page holds the shape.

## Problem

Job alerts pile up on Today until a session the operator starts triages
them. Many need no operator: a rerun, a restart, a stale lock, or a bug to
file. Nothing triages them unattended, on the machine that raised them.

## Approach

`local-night-triage/night-triage.sh run`, a cron job at 08:40 on every
machine, bounded at 45 minutes. It follows `local-nightly-review`'s shape:
`lib/bounded.sh` steps and one `claude -p` on Sonnet with an allow list.

The alert writes stay in code, not in the model. `night_triage.py` claims
this machine's alerts before the model runs and settles them from its JSON
reply after it. A report takes no status through `sd task status` (the
pack refuses every move of the `report` kind), so the claim is a note
and the settle note releases it. So the tests can prove each rule with a fake `sd` and a fake
`claude`, and the model's tools hold no `sd task status` and no acknowledge.

Rejected: give the model `sd task status` and `sd reports acknowledge` and
let the prompt carry the rules. A test could then check only the prompt, and
a bad reply could acknowledge an alert with no evidence.

## Non-goals

- Alerts of other machines: a run reads only reports that name its host.
- Installing the private `.job` on the hub and the satellite: that follows
  the merge.
- A gate on what `rm -f` removes beyond the cron logs folder prefix.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Release | Release note on old claims | Another session took it over | Release note; claim step skips it | `test_a_left_claim_another_session_took_over_is_left_to_it` |
| Release | As above | `sd` fails (outage, locked database) | Row kept; exit 1; next run releases | `test_an_sd_outage_at_release_keeps_the_claim_and_fails` |
| Claim | Row `<id> -`, then a claim note | `task status` refuses a report | Not used: the claim is a note | `test_a_report_is_claimed_by_a_note_not_a_status`, `RealPack` |
| Claim | As above | Another session claimed it | Skipped | `test_another_sessions_claim_note_is_skipped`, `test_a_claimed_alert_is_skipped` |
| Claim | As above | Run dies after the claim | Next run's release note frees it | `test_a_claim_a_crashed_run_left_is_released_by_the_next_run` |
| Claim | As above | Dies between note and row update | Pending row: our claim is latest, so released | `test_a_crash_after_the_claim_still_leaves_the_alert_released` |
| Claim | As above | Note commits, its answer is lost | Released when this run settles | `test_a_claim_whose_answer_is_lost_is_given_back_in_the_same_run` |
| Claim | As above | Pending row left by a crash | Next release reconciles it | `test_a_pending_claim_row_a_crash_left_is_reconciled` |
| Context | `sd today` read | `sd today` fails or refuses its arguments | Claims released, no note; exit non-zero | `test_a_failed_sd_today_fails_the_run_and_gives_the_claims_back` |
| Triage | Repairs; filed items | `claude -p` fails or times out | Claims released, no note; exit non-zero | `test_a_failed_model_run_leaves_the_alerts_as_they_were` |
| Triage | `state/<date>.reply` | Reply holds no `alerts` object | Claims released, no note; exit non-zero | `test_an_unreadable_reply_leaves_the_alerts_as_they_were` |
| Settle | One note (releases the claim) | Run dies before the note | Row kept; next run's release note frees it | `test_a_claim_a_crashed_run_left_is_released_by_the_next_run` |
| Settle | As above | A rerun moved the revision | Re-read; the ingest's writes are judged | `test_a_rerun_that_touches_the_alert_still_settles_it` |
| Settle | As above | A writer slips in before the note | `--if-revision` refuses; judged again | `test_a_followup_added_before_the_note_is_judged_again` |
| Settle | As above | A person wrote during the run | Left to them, with a note | `test_another_writer_during_the_run_leaves_the_alert_to_them` |
| Settle | As above | `sd` fails (outage) | Row kept; exit 1; next run releases | `test_an_sd_outage_while_settling_keeps_the_claim_and_fails` |
| Settle | One note | A fix lacks evidence for a followup | Note says why; not acknowledged | `test_a_fix_without_evidence_for_each_followup_stays_open` |
| Settle | One note | A person wrote an open followup | Note names it; not acknowledged | `test_a_followup_a_person_wrote_blocks_the_acknowledge` |
| Settle | One note | A rerun added a followup | Re-read; unmatched followup stays open; not acknowledged | `test_a_followup_added_during_the_run_blocks_the_acknowledge` |
| Settle | Acknowledged | A writer slips in after the note | `--if-revision` refuses; note says so; stays open | `test_a_followup_added_before_the_acknowledge_is_not_resolved` |
| Settle | Acknowledged, followups resolved | None beyond `sd` refusing | Note stays; exit non-zero; operator sees it | `test_a_fixed_alert_with_evidence_is_acknowledged` |

Every `sd` call the job makes parses with the pinned pack's parser
(`test_every_sd_call_parses_with_the_pinned_pack`), and the fake `sd`
refuses an unknown flag as argparse does. `RealPack` runs the job against
the pinned pack's real `sd` and library: a report its ingest opened is
claimed, noted and acknowledged.

## Acceptance criteria

- [ ] `make test SUITE=night-triage` passes with 0 failures.
- [ ] The allow list holds no merge, push, sudo, `sd task status`,
      acknowledge, install, mail, Slack or MCP tool
      (`test_forbidden_tools_are_absent_from_the_allow_list`).

## Risks

- The model's "fixed" is a claim; the evidence lines in the note are what
  the operator can check. A fix with no evidence is never acknowledged.
- `cron-jobs.sh run <job>` reruns any job; the ruling allows a rerun.
- `rm -f` is limited to the cron logs folder by prefix only, accepted.
