---
title: claude-mem provider chain
created: 2026-09-25
---
# PRD — claude-mem provider chain

## Problem

`claude.sh mem-pro-watchdog` toggles claude-mem's observer between two
providers: the cmem Pro gateway (`openrouter`) and the Claude subscription
(`claude`). The operator added a third, Gemini, on 2026-09-14. Since then the
watchdog reports "not managing claude-mem here" and does nothing, because
`CLAUDE_MEM_PROVIDER` is `gemini`.

The live state on 2026-09-25: `CLAUDE_MEM_PROVIDER=gemini`, the Pro allowance
exhausted (402) since 2026-09-12, and a `seven_day` quota cooldown armed for
`claude`. When Gemini's free quota runs out, nothing moves the observer, and
nothing returns it to Pro when the allowance resets.

## What claude-mem 13.25.3 does (read from `worker-service.cjs`)

- The worker re-reads `~/.claude-mem/settings.json` at every generator start.
  A changed `CLAUDE_MEM_PROVIDER` needs no restart.
- It runs Gemini only when `CLAUDE_MEM_PROVIDER` is `gemini` and a key sits in
  `CLAUDE_MEM_GEMINI_API_KEY` or in `GEMINI_API_KEY` of `~/.claude-mem/.env`.
  It reads that file, not its process environment. Without a key it silently
  runs `claude`.
- A Gemini error body with `RESOURCE_EXHAUSTED` or "quota exceeded" is filed
  as `quota_exhausted`. The worker then arms `quota-cooldown.json` with
  provider `gemini`, and `observer-health.json` names `gemini`.
- The settings file keeps keys the worker does not know. A new
  `CLAUDE_MEM_PROVIDER_CHAIN` key survives the worker's reads.

## Requirements

- **R1 — an ordered chain.** `CLAUDE_MEM_PROVIDER_CHAIN` in `settings.json`
  lists providers, highest priority first. Absent, it is
  `openrouter,claude`, and the watchdog behaves as it did before.
- **R2 — enabled means listed and credentialed.** `openrouter` needs a
  `cm_pro_` token, the gateway URL and the same token as the OPENROUTER key.
  `gemini` needs a key where the worker reads one. `claude` needs nothing.
- **R3 — fail down.** The current provider is out when the worker reports it
  exhausted and a cheap probe confirms it. `claude` has no cheap probe; a
  cooldown entry the worker still enforces is the answer. The observer moves
  to the next enabled provider down the chain that answers. With none, it
  stays and notifies once.
- **R4 — fail up.** Each run probes the enabled providers above the current
  one. The observer returns to one after `CLAUDE_MEM_PRO_RETURN_DELAY_MIN`
  (60) minutes of continuous success, counted per provider.
- **R5 — unknown never moves state.** A probe without a clear answer moves
  nothing.
- **R6 — the exit contract holds.** A provider outside the chain is
  unmanaged and exits 0. The two fault exits stay non-zero.
- **R7 — no key is written.** A switch writes `CLAUDE_MEM_PROVIDER` only, plus
  the cleared `CLAUDE_MEM_PRO_FALLBACK_AT` on a return to `openrouter`.
  Writes stay atomic and mode 600.
- **R8 — the state file migrates.** The first `pro-watchdog.json` shape
  (`mode` pro|fallback, scalar `okSince`) is read and rewritten.

## Acceptance criteria

1. `claude.sh test` passes, with the chain cases red on `origin/main`.
2. The default chain moves Pro to Claude and back, as before.
3. The chain `openrouter,gemini,claude` moves Pro to Gemini, Gemini to
   Claude, and Claude back to Gemini after the delay.
4. Gemini exhausted with Claude in cooldown stays put and notifies once
   across two runs.
5. A Gemini key only in `.env` counts; one only in the process environment
   does not.
6. A dry run against a copy of the live data directory with the chain added
   exits 0 and writes nothing.

## Out of scope

- Renaming the job or the subcommand. Both keep their Pro-only names, so the
  cron registry and the report history stay joined.
- Editing the live `settings.json`. The operator adds the chain after merge.
- Reading the `window` of a cooldown entry. The watchdog uses the worker's
  own 30-minute refusal window instead; see `design.md`.
