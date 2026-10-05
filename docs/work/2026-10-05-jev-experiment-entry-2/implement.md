---
title: Jev experiment, journal entry 2 — keep, swap or drop the review-tier model
created: 2026-10-05
item: sd:2764
---
# Implement — Jev experiment, journal entry 2

The scripts go in one file, `local-jev/jev_experiment.py`, one verb per step.
Its tests go in `local-jev/tests/test_jev_experiment.py`, which the existing `local-jev` suite line already runs.
The pre-registration is `docs/experiments/2026-10-05-jev-entry-2-preregistration.md`; `sd-docs-lint` keeps a work folder to the three planning files.
Each code step is test first: the new test fails before the step.
No test calls a hosted endpoint; hosted and Kev calls run against loopback stubs, as `jev.sh test` does.

**Dependencies on sd:2761.** None of steps 0 to 12 needs it.
The replay calls `jev.post`, `kev_arm` and `haiku_arm` directly, so it needs neither the shadow switch, the `--baseline` row nor `--local-only`.
Step 10 uses a draft scanner question until sd:2761 merges, then reruns its state builder against sd:2761's caller (step 10b).
Step 15 needs sd:2761 merged.

| # | Step | Hours | Can start |
|---|---|---|---|
| 0 | Reconcile entry 1's extraction | 1.5 | now |
| 1 | Faithfulness check: 50 states re-asked of Jev | 2.5 | after 0 and this registration commit |
| 2 | Population: every reviewed head since 2026-09-09 | 3 | after 0 |
| 3 | Rules and heuristic arms | 1.5 | now |
| 4 | Objective labels | 1 | after 2 |
| 5 | Replay harness for all arms | 3 | after 1 |
| 6 | Registration of the remaining predictions (operator) | 0.5 | after 1–5 |
| 7 | Replay run | 3 wall | after 1 passes and 6 |
| 8 | Blinded label kit | 1.5 | after 7 |
| 9 | Hand labelling (operator) | 3 | after 8 |
| 10 | Scanner benchmark | 3 | now |
| 10b | Scanner question from sd:2761 | 0.5 | after sd:2761 |
| 11 | Injection test | 2 | after 5 |
| 12 | Analysis and results note | 3 | after 7, 9, 10, 11 |
| 13 | Privacy review of the post's numbers | 1 | after 12 |
| 14 | Gate and close | 0.5 | after 12 |
| 15 | Live shadow cross-check | 1 | after sd:2761 |

Agent build time: about 25 hours. Operator time: about 3.5 hours. Wall time for the runs: about 4 hours.

## Steps

0. **Reconcile entry 1's extraction** (`reconcile`). No model call.
   Run both extractions in `design.md` and any variant needed (which key, which pass, which reading, which window bounds in UTC or MDT) until one gives 779 heads, 47 moves, 41 down and 28 to `skip`.
   Tests: a fixture checkpoint whose second pass reviews a later head counts once under each extraction, as each defines it.
   Check: the matching query and its counts go on sd:2764. If none matches, the note says so, entry 1's numbers are flagged for correction before entry 2 is published, and the operator names which set "the 28" in O1 means.

1. **Faithfulness check** (`faithful`). The operator's O4.
   Rebuild 50 states from heads with a recorded reading and a stored subject, seeded with 2764, 25 of them from heads where Jev moved the tier.
   Ask Jev once each through `jev.post`, both comparison switches unset, writing to the isolated ledger.
   Tests against a loopback stub: the rebuilt payload equals the pack's `_jev_state` output for the same inputs; the Jev suffix is stripped from the reason; a state with `/Users/` halts before the post.
   Check: at least 46 of 50 match the recorded tier. At 45 or fewer, stop: fix the rebuild, record the first attempt on sd:2764, and draw 50 new states.
   The 50 answers are kept and reused by step 7.

2. **Population** (`population`). Every distinct reviewed head since 2026-09-09, keyed `coalesce(reviewed_head, head)`.
   States from the checkpoint where a subject is stored; from git and the pinned pack's router where it is not, flagged `rebuilt-from-git`.
   Tests: a duplicate head, an invalid JSON body, a declined reading, a head with no stored subject, a head whose objects are gone.
   Check: about 2,222 heads, each with a state or a counted reason why not; the paper subset from step 0 is marked.

3. **Rules and heuristic arms** (`offline`). The rules arm reads the recorded routed tier, or the recomputed one; the heuristic is the function in `design.md`, unchanged.
   Tests: the zero-line shape, the asset shape, a risky path, a docs-and-tests change under 20 lines.
   Check: the heuristic's file hash is in the manifest; on the paper subset, it answers `skip` for every zero-line head.

4. **Objective labels** (`labels objective`). blocking@head and "too low" from the checkpoints, written to `population.jsonl`.
   Tests: a head with only advisory findings, a head whose first pass did not complete, a later head's finding that must be ignored.
   Check: the blocking rate on the paper subset is within a point of the plan-time 37%.

5. **Replay harness** (`replay --arm jev|kev|haiku`). Step 1's call path, extended to Kev and Haiku in-process on the same redacted payload.
   Ledger isolation, traces, cost cap, privacy halt and the one-retry rule are in this step.
   It refuses to start while a `PREDICTION (operator):` line in the pre-registration is blank, records the registration SHA, and skips heads step 1 already asked.
   It runs hosted arms beyond the paper subset only with `--hosted-scope all`, which the operator's confirmation on sd:2764 allows.
   Tests against loopback stubs: one row per head per arm; a second run without `--rerun` refuses; the cap halts at the configured spend; no live-stage row is written.
   Check: `jev.sh test` green; a 5-head dry run against the stubs writes 15 answers and 15 spans.

6. **Registration** (operator). Fill the remaining predictions in the pre-registration and commit.
   Check: `grep -c '^PREDICTION (operator): _*$' docs/experiments/2026-10-05-jev-entry-2-preregistration.md` prints 0; the SHA is on sd:2764.

7. **Replay run.** `genai-traces.sh status` and `kev.sh status` exit 0 first. Local arms run on every head; Kev first, then Jev, then Haiku.
   Run in the foreground or watch the log with Monitor.
   Check: each arm's file has one line per head in its scope; no-answer counts are in the manifest; spend is under $10.

8. **Blinded label kit** (`labels sheet`). Sheet, Markdown copy and sealed key per `design.md`, seed 2764.
   Tests: no arm name and no finding in the sheet; the key maps every row; the order is stable for the seed.
   Check: the row count is min(150, disagreements).

9. **Hand labelling** (operator). Fill `label` and `sure`; put the sheet's SHA-256 on sd:2764; only then open the key.

10. **Scanner benchmark** (`scanner`). Generator and Kev-only runner per `design.md`.
    Tests: the generator reads no environment variable and no file; the runner refuses a non-loopback URL; `TYPESAFE_API_KEY` is absent from the runner's environment.
    Check: 1,200 realistic-fake hits and 1,600 others answered; no request left loopback, shown by the stub log in tests and by Kev's access log in the run.
    10b. After sd:2761 merges, build the state with its scanner caller's shape and rerun once; the draft run stays as a reported second run.

11. **Injection test** (`injection`). 50 public base states, 4 variants, 2 echo forms, every arm; 20 public findings for the secondary test.
    Tests: every base state comes from one of the two public repositories; each variant differs from its twin by one path.
    Check: 400 variants per arm answered, plus 20 finding pairs per model arm.

12. **Analysis** (`report`). The analysis plan in the pre-registration, in its order, into `results.md` with the query for each number.
    Check: every hypothesis is held, failed or not decidable, with its n against the sample-size table; the decision rule's line is named.

13. **Privacy review.** The eight rules in `design.md` on the draft's numbers and quotes.
    Check: `scan-for-secrets.sh` and the leak guard's patterns report zero hits on the draft.

14. **Gate and close.** `make check` through `sd gate check --base main`; `sd-docs-lint` from the root; the results summary on sd:2764.

15. **Live shadow cross-check** (after sd:2761). Compare the replay's agreement rates with the live shadow agreement `judgments compare` reports for `JEV_SD_REVIEW`.
    Check: the note states both rates and their windows; a gap above 10 points is named as a finding.

## Open questions for the operator

1. Which Haiku transport and key: `anthropic` (`JEV_COMPARE_ANTHROPIC_KEY`) or `openrouter`? `claude-cli` is excluded for latency and tokens.
2. Ledger isolation: an experiment database through `JEV_METER_DB`, or a separate caller and stage in the live ledger?
3. The approved re-send covers the 779. Replaying every head since 2026-09-09 re-sends about 1,450 more heads' private paths to TypeSafe and Anthropic. Confirm, or keep hosted arms to the paper subset?
4. Keep or strike the planner's added switch condition: Kev's p95 wait at most 5 s (H5).
5. O1 was given after the plan showed 0 of the 28 with a blocking finding. Keep O1 as registered, or restate it on the hand labels only?
6. Should the heuristic's `skip` for binary-asset-only changes stay? It encodes entry 1's PNG case and could mask a breaking asset removal.
