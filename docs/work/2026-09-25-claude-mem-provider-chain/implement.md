---
title: claude-mem provider chain
created: 2026-09-25
---
# Implement — claude-mem provider chain

## Steps

1. **Tests first.** `local-claude/tests/test_provider_chain.py` runs the real
   script with `--apply` against a scratch data directory and a stub server
   on `127.0.0.1` for the gateway and Gemini.
   Check: the new cases fail on `origin/main`.
2. **The watchdog.** Rewrite `mem_pro_watchdog` in `local-claude/claude.sh` to
   the design's run. Help text, `local-claude/README.md` and the job's comment
   follow.
   Check: `claude.sh test` green; each mutant below turns a case red.
3. **The citation.** The `unmanaged` docstring cited `cron-jobs.sh` by line.
   It now cites a snippet, and its `KNOWN_LINE_INTO_CODE` entry leaves.
   Check: `python3 tests/test_citations.py` green.
4. **Rollout, by the operator after merge.** Add one key to
   `~/.claude-mem/settings.json`:
   `"CLAUDE_MEM_PROVIDER_CHAIN": "openrouter,gemini,claude"`.
   Check: the next cron run logs a provider line, not "not managing".

## Verification

Named before the work:

- `sh local-claude/claude.sh test` — every case passes; the chain cases fail
  on `origin/main`.
- Five mutants, each run through the suite: the `.env` read removed, the
  stuck-once guard removed, the `claude` cooldown check removed, the return
  delay removed, the down-step streak reset removed. Each must turn at least
  one case red.
- A dry run against a copy of `~/.claude-mem` with the chain added exits 0,
  and the copy's `settings.json` is unchanged.
- `python3 tests/test_citations.py` and `sd-docs-lint` clean.

## Log

2026-09-25 — built on `feat/claude-mem-provider-chain`. Fail-first: on
`origin/main` the suite ran 27 cases, `FAILED (failures=12, errors=3)`. Those 15
are new cases; the other 5 new ones pass there by design, because they pin
behaviour `origin/main` already has.
The four mutants of the first commit each failed at least one case. The folder is not
registered; `sd-db.sh work register` runs from `~/repos/system` after merge.

2026-09-25 — local review round 1 (`sd-review --scope branch`, codex, tier
deep), two blocking findings, both accepted:

- A `claude` cooldown entry of any age blocked the move down. Only a success
  with `claude` removes it, so a stale entry blocked forever. Fixed: an entry
  counts for the worker's 30-minute refusal window only. Test
  `test_a_claude_cooldown_the_worker_no_longer_enforces_does_not_block`
  failed on the first commit with `'gemini' != 'claude'`.
- A failure seen in the down step left the candidate's streak alone, so its
  next success returned at once. Fixed: any non-ok check ends the streak.
  Test `test_a_failure_seen_looking_down_ends_that_providers_streak` failed
  on the first commit with the old streak still set.

2026-09-25 — Copilot review, one finding, accepted: a current provider that
its own settings disable was notified as a rejected key or a lapsed
subscription. It now carries the verdict `unusable`. Two tests failed on the
second commit with `'rejected' unexpectedly found`.

2026-09-25 — `sd-ship prepare` local review, two blocking findings, both
accepted:

- A candidate whose probe answered was selected during its worker cooldown,
  so the worker refused it and the observer stalled. Fixed: a candidate with
  a cooldown entry under 30 minutes old is `exhausted` without a probe.
  `test_a_candidate_the_worker_still_refuses_is_passed_over` failed with
  `'gemini' != 'claude'`.
- A current provider that was out, with nothing below answering, waited the
  full return delay for a provider above that answered. Fixed: the down step
  then checks the providers above and moves at once; the delay applies only
  while the current provider works.
  `test_a_stuck_provider_moves_up_at_once_to_one_that_answers` failed with
  `'gemini' != 'openrouter'`.
