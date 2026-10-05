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

**Dependencies on sd:2761.** None of steps 1 to 11 needs it.
The replay calls `jev.post`, `kev_arm` and `haiku_arm` directly, so it needs neither the shadow switch, the `--baseline` row nor `--local-only`.
Step 9 uses a draft scanner question until sd:2761 merges, then reruns its state builder against sd:2761's caller (step 9b).
Step 14 needs sd:2761 merged.

| # | Step | Hours | Can start |
|---|---|---|---|
| 1 | Population | 2 | now |
| 2 | Rules and heuristic arms | 1.5 | now |
| 3 | Objective labels | 1 | now |
| 4 | Replay harness | 4 | now |
| 5 | Registration (operator) | 0.5 | after 1–4 |
| 6 | Replay run | 2 wall | after 5 |
| 7 | Blinded label kit | 1.5 | after 6 |
| 8 | Hand labelling (operator) | 3 | after 7 |
| 9 | Scanner benchmark | 3 | now |
| 9b | Scanner question from sd:2761 | 0.5 | after sd:2761 |
| 10 | Injection test | 2 | after 4 |
| 11 | Analysis and results note | 3 | after 6, 8, 9, 10 |
| 12 | Privacy review of the post's numbers | 1 | after 11 |
| 13 | Gate and close | 0.5 | after 11 |
| 14 | Live shadow cross-check | 1 | after sd:2761 |

Agent build time: about 21 hours. Operator time: about 3.5 hours. Wall time for the runs: about 3 hours.

## Steps

1. **Population** (`population`). Read `sd.db` read-only (`mode=ro`), select per `design.md`, write `population.jsonl` and its hash.
   Rebuild each state with the pack's state shape; verify paths and lines with `git diff --numstat` where the objects exist.
   Tests: a fixture checkpoint set with a duplicate head, an invalid JSON body, a declined reading and the Jev suffix on the reason.
   Check: the count is 779, or the delta and its cause are written on sd:2764; 47 disagreements and 28 `standard` to `skip`.

2. **Rules and heuristic arms** (`offline`). The rules arm reads the recorded routed tier; the heuristic is the function in `design.md`, unchanged.
   Tests: the 28-shape (no paths, zero lines), the asset shape, a risky path, a docs-and-tests change under 20 lines.
   Check: the heuristic's file hash is in the manifest; on the population, it answers `skip` for all 28 zero-line heads.

3. **Objective labels** (`labels objective`). blocking@head and "too low" from the checkpoints, written to `population.jsonl`.
   Tests: a head with only advisory findings, a head whose first pass did not complete, a later head's finding that must be ignored.
   Check: the blocking rate is within a point of the plan-time 37%.

4. **Replay harness** (`replay --arm jev|kev|haiku`). For each head: build the payload with `jev.build_payload`, redact, post.
   Kev and Haiku run in-process on the redacted payload. Ledger isolation, traces, cost cap, privacy halt and the one-retry rule are in this step.
   It refuses to start while a `PREDICTION (operator):` line in the pre-registration is blank, and records the registration SHA.
   Tests against loopback stubs: one row per head per arm; a second run without `--rerun` refuses; a state with `/Users/` halts before the post; the cap halts at the configured spend; no live-stage row is written.
   Check: `jev.sh test` green; a 5-head dry run against the stubs writes 15 answers and 15 spans.

5. **Registration** (operator). Fill every prediction in the pre-registration and commit.
   Check: `grep -c '^PREDICTION (operator): _*$' docs/experiments/2026-10-05-jev-entry-2-preregistration.md` prints 0; the SHA is on sd:2764.

6. **Replay run.** `genai-traces.sh status` exits 0 and `kev.sh status` exits 0 first. Kev runs first, then Jev, then Haiku.
   Run in the foreground or watch the log with Monitor.
   Check: each arm's file has one line per head; no-answer counts are in the manifest; spend is under $10.

7. **Blinded label kit** (`labels sheet`). Sheet, Markdown copy and sealed key per `design.md`, seed 2764.
   Tests: no arm name and no finding in the sheet; the key maps every row; the order is stable for the seed.
   Check: the row count is min(150, disagreements).

8. **Hand labelling** (operator). Fill `label` and `sure`; put the sheet's SHA-256 on sd:2764; only then open the key.

9. **Scanner benchmark** (`scanner`). Generator and Kev-only runner per `design.md`.
   Tests: the generator reads no environment variable and no file; the runner refuses a non-loopback URL; `TYPESAFE_API_KEY` is absent from the runner's environment.
   Check: 1,200 realistic-fake hits and 1,600 others answered; no request left loopback, shown by the stub log in tests and by Kev's access log in the run.
   9b. After sd:2761 merges, build the state with its scanner caller's shape and rerun once; the draft run stays as a reported second run.

10. **Injection test** (`injection`). 50 public base states, 4 variants, 2 echo forms, every arm; 20 public findings for the secondary test.
    Tests: every base state comes from one of the two public repositories; each variant differs from its twin by one path.
    Check: 400 variants per arm answered, plus 20 finding pairs per model arm.

11. **Analysis** (`report`). The analysis plan in the pre-registration, in its order, into `results.md` with the query for each number.
    Check: every hypothesis is held, failed or not decidable; the decision rule's line is named.

12. **Privacy review.** The eight rules in `design.md` on the draft's numbers and quotes.
    Check: `scan-for-secrets.sh` and the leak guard's patterns report zero hits on the draft.

13. **Gate and close.** `make check` through `sd gate check --base main`; `sd-docs-lint` from the root; the results summary on sd:2764.

14. **Live shadow cross-check** (after sd:2761). Compare the replay's agreement rates with the live shadow agreement `judgments compare` reports for `JEV_SD_REVIEW`.
    Check: the note states both rates and their windows; a gap above 10 points is named as a finding.

## Open questions for the operator

1. Which Haiku transport and key: `anthropic` (`JEV_COMPARE_ANTHROPIC_KEY`) or `openrouter`? `claude-cli` is excluded for latency and tokens.
2. Ledger isolation: an experiment database through `JEV_METER_DB`, or a separate caller and stage in the live ledger?
3. The decision rule defaults to drop when no arm shows a signal. Accept that, or keep collecting in shadow under sd:2761 instead?
4. The second population (about 185 heads with no historic reading) re-sends more private paths. Is that inside the approved one re-send?
5. Should the heuristic's `skip` for binary-asset-only changes stay? It encodes entry 1's PNG case and could mask a breaking asset removal.
