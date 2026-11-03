---
title: Machines coordinate through the workflow database, not remote sessions
created: 2026-11-03
item: sd:7324
branch: docs/sd-7324-db-coordination
---

# Design — db-coordination

This page covers system sd:7324.
Binding ruling: database only; SSH stays off (note #41395).

## Problem

Several rules send work from one machine to another through a remote Claude session (`ListAgents`, then `SendMessage`).
That path fails in three ways seen on 2026-11-03:

- A Remote Control session on a satellite gives the sender no reply path.
- Auto mode there refuses persistence edits, so the operator ran the scripts by hand.
- A message reaches nobody when no session runs on the receiver; nothing records that it was lost.

The database already reaches every machine over the tailnet, and jobs on each machine read it.
Three mechanisms prove the shape:

- `sys.cswap_account` (sd:7323): the hub's `cswap.sh sync` writes a plugin config key; every other machine's job reads it.
- `refresh.request` (sd:5108): the dashboard writes a setting row; each satellite's `repo-sync.sh follow` runs it and writes `refresh.result.<host>`.
- `place_tiles.request.<host>...` (sd:6387): the same request and result rows, addressed to one host.

## Hop inventory

Read from disk on 2026-11-03: `local-agent-prompt/prompt/shared.md`, the machine-local sections of the global
instruction file, `.claude/rules/`, this repository's scripts, and the command pack (skills, `WORKFLOW.md`, `bin/`, `lib/`).
The pack holds no cross-machine session hop; `ListAgents` in `sd-next` and in the enqueue rule lists agents on the same machine.

| # | Source | Direction | Trigger | Payload | Receiver does |
| --- | --- | --- | --- | --- | --- |
| H1 | `shared.md:52`, pinned by `local-agent-prompt/tests/test_agent_prompt.py:328` | hub lead to `work-<repo>` | a lane entry ends failed in a satellite-hosted lane | item, step, reason | fixes the branch, retries |
| H2 | global file, Mail and Slack section | satellite to hub | a satellite task needs mail, Slack, Notion or Drive | what to read, draft or fetch | does it with hub credentials, replies |
| H3 | global file, Machine roles section | satellite to hub | a satellite session finds system or pack work | the work | files or does it on the hub |
| H4 | `shared.md:189-190` | any to any | the general rule: reach another machine through its session | anything | anything |
| H5 | `docs/work/2026-10-25-hub-failover-full-role/design.md:158-162`, `docs/work/2026-10-13-config-to-db/design.md:96,197` | hub lead to satellite | a one-off step on a satellite: a hand edit of its instruction file, `sd-db repo seed --clones`, reading its config keys | a command | runs it, reports |
| H6 | `local-cswap/cswap.sh sync` | hub to satellites | the hub's account changes | `sys.cswap_account` | done in sd:7323; the worked precedent |

## Approach

Each hop becomes a row a job or a session on the receiver reads.
Nothing pushes; the receiver's next run or the next session start finds the row.
Every replacement reuses an existing mechanism; the one new value is a ref source, `hub` (H2).

| # | Replacement | Reuses | New |
| --- | --- | --- | --- |
| H1 | The lane already writes the failure as a note on each item (`note_ended`, sd:5963). For a lane whose `repo.lane_host` is not the hub, write it as an open `followup`, so `sd today` and the dashboard show it on every machine until a session resolves it. The session that builds an item enqueues and watches it on the lane host; the hub lead does not enqueue a satellite-hosted lane. | note kinds, `lane watch`, `sd today` followups | none |
| H2 | The satellite files `sd task add "<request>" --kind followup --belongs-to <repo> --ref hub:<host>-<slug>`. The hub's `sd today` lists open `hub:` items under Attention; a hub session claims one as it claims an alert (`sd task status N in_progress`), does the work, writes the answer as a note, and closes it. The satellite reads the answer with `sd task show N`: the reply path a session lacks. | items, `--ref`, Attention, the alert claim rule | ref source `hub` |
| H3 | The satellite files the item `--belongs-to` the system or pack checkout. It shows in the hub's `sd today` with no other step. Delete the `SendMessage` branch of the rule. | items | none |
| H4 | Rewrite: reach another machine through the database (an item, a note, a setting row or a plugin key its jobs or sessions read); never SSH and never a remote session. Delete the session-name line. | rule text | none |
| H5 | A one-off satellite step becomes a `machine-setup` stage or a job the `refresh.request` row runs (sd:5108), with its result in the job's report. A read of a satellite's state comes from its job report, not a session. The `shared.md` block already reaches every machine through the prompts stage. | `refresh.request`, cron reports | none |
| H6 | None. sd:7352 is its precondition: every machine registers the `sys` plugin. | plugin config keys | none |

Rejected: a generic setting row that runs a named command on a host.
It is a remote-execution channel with the risk SSH carries, and each need so far fits a stage or a job.

Rejected: a new item kind `hub-request` for H2.
A ref source filters the same rows with no schema change.

## Non-goals

- Same-machine `ListAgents` and `SendMessage` between a lead and its teammates.
- Any push or wake-up of an idle session on another machine.
- Moving mail, Slack, Notion or Drive credentials off the hub.
- Failover: a promoted work satellite reads the same rows as the hub it replaces.

## Failure table

S2 and S3 move state in steps.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| S2 lane ends failed | entry `failed`; followup note on each item | the note write fails | the entry stays `failed` in the queue; `note_ended` names the failed write in the run's answer | a fake `note` that raises; the entry still ends `failed` and the answer names it |
| S2 session resolves | followup resolved | no session runs on the lane host | the followup stays open on the dashboard and in `sd today` on every machine | a satellite-hosted failure shows in `sd today` on a hub-role database |
| S2 hub lane fails | comment note, as today | a hub lane gets a followup by mistake | none needed; the test fails first | a hub-hosted failure writes `comment`, not `followup` |
| S3 file request | item with `hub:` ref | the hub does not answer | `sd task add` exits non-zero with `hub_unavailable`; the session files again later | none: existing `sd task add` behaviour |
| S3 claim | `in_progress` | two hub sessions claim at once | the second sees `in_progress` and skips it, as with alerts; a race leaves two workers on a draft, never a send | none: the alert claim rule, unchanged |
| S3 work, then answer | answer note | the session stops after the claim | the item stays `in_progress` in Attention with its age; a later session takes it over | an `in_progress` `hub:` item older than a day still lists |
| S3 close | `done` | the answer lands but `done` does not | the next hub session sees the answer note and closes the item | an open `hub:` item with an answer note lists; closing it removes it |

## Slices

1. **S1 rules (system).** Rewrite `shared.md` lines for H1, H3, H4 and H5; add the H2 line; change the pin in `test_agent_prompt.py` fail-first. Every machine gets it at its next prompts stage. The operator deletes the two machine-local `SendMessage` lines (H2, H3) from each global file outside the block, or they move into `shared.md` (Q4).
2. **S2 lane followups (pack).** `note_ended` writes `followup` when the lane host is not the hub. Hub lanes keep `comment`.
3. **S3 hub requests (pack).** `sd today` Attention on the fence holder lists open items whose ref source is `hub`. S1's H2 line lands with or after this slice.

Each slice leaves every machine working: S1 alone keeps today's behaviour for H1 and H2 until S2 and S3 land, since the old notes still exist.

## Acceptance criteria

- [ ] `grep -n SendMessage local-agent-prompt/prompt/shared.md` prints only same-machine teammate lines.
- [ ] `make test SUITE=agent-prompt` passes, with the new pin failing on `origin/main` first.
- [ ] Pack: a satellite-hosted lane failure writes an open `followup`; a hub-hosted one writes a `comment`.
- [ ] Pack: an open `hub:` item shows under Attention on the hub and in no satellite's Operations section.
- [ ] One satellite files a `hub:` request and reads the hub session's answer with `sd task show`, no session message sent.

## Risks

- No push: a request waits until a hub session starts. Accepted: the operator starts the hub session daily, and the item is on the dashboard.
- Followups add notes to resolve on satellite lanes. Accepted: they are rare, and a lost failure costs more.
- The `hub` ref is a convention; a mistyped ref hides a request. S3's test pins the exact source.
- H5 needs a stage per one-off need, which may feel heavy for a single read. Accepted over a remote-exec row.

## Open questions

- Q1 H1: followup note on a satellite-hosted lane failure (Recommended), or a row in that host's Operations section.
- Q2 H2: ref source `hub` read by the hub's Attention (Recommended), or a new item kind.
- Q3 H5: a stage or job per one-off need (Recommended), or a generic run-on-host setting row.
- Q4 Machine-local hop lines: move them into `shared.md` so the prompts stage distributes them (Recommended), or the operator edits each machine's file by hand.
