---
title: Run a local Kev and a cheap frontier model beside every live Jev call
created: 2026-10-01
item: sd:2366
---
# PRD — Kev and Haiku arms beside every live Jev call

## Problem

The Jev evaluation paper needs paired data. For one decision it must compare
TypeSafe's hosted Jev, a local open-weights model of the same shape (Kev), and
a cheap frontier model (Claude Haiku 4.5) asked through a prompt.

Today the meter (sd:2087) records one `judgment` row per Jev call. Nothing asks
another model the same question. A comparison run after the fact cannot
reproduce the live state, because `jev` records no content by design.
So the comparison must happen at call time, on the request that leaves the
machine.

## Requirements

1. `local-kev` runs Kev's own server on this Mac, bound to `127.0.0.1`.
   It follows the repository conventions: one entrypoint `kev.sh`, `install`,
   `serve`, `agent-install`, `agent-uninstall`, `status` (0 healthy, 3 not
   installed or not running, 1 up and broken), `test` and `help`.
2. The default checkpoint is Kev-4B pinned to the `v1.0` Hub revision.
   `KEV_MODEL` in the config folder selects another.
3. Every `jev` call that sends a request to Jev also sends the same redacted
   request to a Kev arm and to one Haiku arm.
4. The caller sees exactly what it saw before: the same stdout, the same exit
   code, and no added wait beyond starting one process.
5. Four Haiku transports are configurable: OpenRouter, the Anthropic Messages
   API, `claude -p --model haiku`, and Baseten. `JEV_COMPARE_HAIKU_VIA`
   selects one; unset or an off-word is off.
6. Each arm is opt-in and switched on on its own: unset means off, unlike a
   Jev stage. `JEV_COMPARE_KEV` takes the on-words in `local-jev/jev.py`; the
   off-words switch either arm off. With both off, a Jev call is unchanged.
7. An unreachable Kev or an unkeyed Haiku transport costs the caller nothing
   and records a decline reason.
8. Every arm writes its own `judgment` row with an arm name, its provider and
   model, and the pair id of the Jev row. The row holds the answer as a
   number, the confidence, the per-option probabilities as numbers, tokens,
   wall-clock latency, model-reported latency when the response carries one,
   cost, and the outcome.
9. Kev costs 0. Haiku cost comes from configured per-token prices, or from the
   transport's own reported cost when it reports one.
10. `sd-db.sh judgments compare` prints, per stage and per arm: calls, p50 and
    p95 latency, tokens, cost, agreement with Jev on the same pair, and
    accuracy and Brier score on labelled pairs. `--json` gives the same data
    to the paper's scripts.
11. The same redaction and refusal that guard the Jev request guard every arm.
    The README says plainly that the Haiku arm sends caller state to a second
    third party.
12. `make check` never calls a real endpoint and never loads a launchd agent.

## Out of scope

- Using a Kev or Haiku answer in place of Jev's. The arms only record.
- Running the arms on calls that do not reach Jev: a decline, `status`, or a
  machine with the meter off.
- A labeller. Labels still come from `sd-db.sh judgments label` (sd:2107).
- Fine-tuning Kev.

## Success

- A hung arm does not delay a caller that reads `jev` through a pipe.
- A live call on this machine writes three rows with one pair id.
- `sd-db.sh judgments compare` reads them back per stage.
