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
reply after it. So the tests can prove each rule with a fake `sd` and a fake
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
| Release | Old claims back to their status | Another session claimed it since | Left to it; row dropped | `test_an_alert_another_session_claimed_since_is_left_to_it` |
| Release | As above | `sd` fails (outage, locked database) | Row kept; exit 1; next run releases | `test_an_sd_outage_at_release_keeps_the_claim_and_fails` |
| Claim | Alert `in_progress`; line in `state/claims` | Another session claims first | Revision check refuses; skipped | `test_a_claimed_alert_is_skipped` |
| Claim | As above | Run dies after the claim | Next run's release gives it back | `test_a_claim_a_crashed_run_left_is_released_by_the_next_run` |
| Triage | Repairs; filed items | `claude -p` fails or times out | Claims released, no note; exit non-zero | `test_a_failed_model_run_leaves_the_alerts_as_they_were` |
| Triage | `state/<date>.reply` | Reply holds no `alerts` object | Claims released, no note; exit non-zero | `test_an_unreadable_reply_leaves_the_alerts_as_they_were` |
| Settle | Alert back to `planning` | Run dies before the note | Alert open, no note; next run triages it | release before note, by order in `apply` |
| Settle | As above | A rerun moved the revision | Re-read; still our claim, so released at the new one | `test_a_rerun_that_touches_the_alert_still_settles_it` |
| Settle | As above | `sd` fails (outage) | Row kept; exit 1; next run releases | `test_an_sd_outage_while_settling_keeps_the_claim_and_fails` |
| Settle | One note | A fix lacks evidence for a followup | Note says why; not acknowledged | `test_a_fix_without_evidence_for_each_followup_stays_open` |
| Settle | One note | A rerun added a followup | Re-read; unmatched followup stays open; not acknowledged | `test_a_followup_added_during_the_run_blocks_the_acknowledge` |
| Settle | Acknowledged, followups resolved | None beyond `sd` refusing | Note stays; exit non-zero; operator sees it | `test_a_fixed_alert_with_evidence_is_acknowledged` |

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
