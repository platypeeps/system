---
title: Run a local Kev and a cheap frontier model beside every live Jev call
created: 2026-10-01
item: sd:2366
---
# Implement — Kev and Haiku arms beside every live Jev call

One pull request. Each step is test first: the new test fails against the
code before the step.

## Ledger (`local-sd-db`)

1. Tests: a `kev` and a `haiku` row are accepted; `server_ms` and
   `probabilities` are stored; a `probabilities` value that is not numbers and
   commas is refused; rows written at 016 survive the 017 migration with their
   ids.
   Check: they fail at schema 16.
2. `017_judgment_compare_arms.sql`, `SCHEMA_VERSION = 17`, `ARMS`, the two
   fields in `record`.
   Check: `sd-db.sh test` green.
3. Tests: `compare` returns per-stage, per-arm calls, percentiles, cost,
   agreement and Brier from a fixture of paired rows; the old `judgments`
   report ignores the new arms.
4. `sd_db.judgment.compare`, its text and JSON forms, and
   `sd-db.sh judgments compare`.
   Check: `sd-db.sh test` green.

## Arms (`local-jev`)

5. Tests against loopback stubs: a Kev stub, an Anthropic stub, an OpenAI-
   compatible stub for OpenRouter and Baseten, and a `claude` stub on PATH.
   Each writes the expected row: arm, provider, model, pair, answer,
   probabilities, tokens, cost.
6. Tests: an unkeyed transport writes an `unkeyed` decline and sends nothing;
   a refused Kev writes `unavailable`; a slow Kev past `JEV_COMPARE_TIMEOUT`
   writes `timeout`; an off-word sends nothing and writes nothing.
7. Test: a hung Kev arm does not delay `jev.sh noul` run through a pipe, and
   the caller's stdout is Jev's answer.
8. `jev_compare.py` and the hook in `jev.post`; `jev.sh` exports the new
   variables; `.env.example` lists them.
   Check: `jev.sh test` green.

## Service (`local-kev`)

9. Tests with `launchctl` and `uv` stubbed on PATH: help exits 0, no argument
   exits 1, `status` exits 3 with no checkout and 3 with nothing listening,
   0 against a stub `/v1/models`, 1 against a port that answers something
   else; `agent-install` fills the template with the prefix and calls
   `bootstrap`.
10. `kev.sh`, `kev.plist.template`, `.env.example`, `README.md`, `logs/.gitkeep`,
    the `run_suite` line, and the port in `.claude/rules/services.md`.
    Check: `kev.sh test` green; the preflight's unwired-suite guard passes.

## Gate

11. `make check`, `sd-docs-lint`, `scan-for-secrets.sh`.

## After the pull request

12. On this Mac: `kev.sh install`, download the 4B weights, `kev.sh
    agent-install`, `kev.sh status`, and one real `/v1/systemone` request.
    Record the measured latency in the pull request.
13. After the merge, the operator migrates the store to 017 and reinstalls
    `sd_db` in the command pack's venv. Until then arm rows are dropped.
