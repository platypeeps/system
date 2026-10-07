---
title: Satellite staleness alarm
created: 2026-10-07
item: sd:2918
---
# PRD — satellite staleness alarm

## Problem

The hub lead hands work items to sessions on satellites.
A satellite session can stop without a word: the laptop sleeps, the session waits on a prompt, or a tool hangs.
Nothing on the hub notices. The item stays `in_progress` or `planning`, and the operator finds the stall by asking.

Today the database cannot say which items a satellite holds.
`note.session` holds `SD_SESSION`, which is usually unset.
The hub's serve agent names the satellite's tailnet login and address only in its log, at admit time.
No table records a machine.

## Goal

Alert the operator when an item a satellite holds shows no progress for 3 hours.

## Requirements

1. A satellite declares that it holds an item, and releases it when it hands the item back.
   The declaration names the machine and the branch, and records when it started.
2. Progress is the newest of three timestamps:
   a note on the item, the item's `updated_at`, and the committer date of the claimed branch's head on `origin`.
3. A claim is stale when its newest progress is older than the threshold, 3 hours by default.
   `SD_SATELLITE_STALE_HOURS` overrides the threshold.
4. A claim on an item that is `done`, `blocked` or `ready_to_send` is not checked.
   `blocked` and `ready_to_send` wait on someone other than the satellite.
5. The check runs on the hub only. A satellite refuses it with `HubOnly`.
6. One stale episode sends one alert. A new episode starts only after progress moves.
7. The operator silences a claim until a stated time, or releases it.
8. The check's verb answers convention 6 exit codes, so `local-health-check` reports it.
9. A dry run prints the stale claims and sends nothing.

## Acceptance criteria

- [ ] A claim with a note 2 h old is not stale; one with all signals 4 h old is stale.
- [ ] A pushed commit 1 h old keeps a claim fresh when notes are 5 h old.
- [ ] A `blocked` item, a released claim and a silenced claim send nothing.
- [ ] A second run in the same episode sends nothing; progress followed by a new stall sends again.
- [ ] A satellite refuses the check with `HubOnly`; a satellite writes a claim over the wire.
- [ ] The `status` verb exits 0, 1 and 3 as convention 6 states.
- [ ] `make check` passes.

## Out of scope

- Automatic claims from the pack's `sd task status` or worktree start; a later pack item.
- Gate receipts as a progress signal; they live in the pack's store, and a gate run is followed by a push or a note.
- Restarting or messaging the stalled session.

## Log

- 2026-10-07 design record written on a satellite; open questions sent to the hub lead for the operator.
