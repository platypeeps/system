---
title: claude-mem provider chain
created: 2026-09-25
---
# Design — claude-mem provider chain

## One run

Every run reads `settings.json`, the chain and the state, then takes at most
one step.

1. **Unmanaged checks.** No `settings.json` exits 0. An unreadable one exits
   1. An OPENROUTER key that differs from a `cm_pro_` token exits 1, when
   `openrouter` is in the chain. A `CLAUDE_MEM_PROVIDER` outside the chain
   exits 0. A chain with fewer than two enabled providers exits 0 and names
   why each one is off.
2. **Fail down.** The worker's signal for the current provider is a
   `quota-cooldown.json` entry, or `observer-health.json` reporting
   `quota_exhausted` for it after the last success. With a signal, the
   watchdog checks the provider. `exhausted` or `inactive` confirms it. A
   current provider without credentials counts as out too, because the worker
   then runs `claude` unannounced. Each enabled provider below is checked in
   order, and the first `ok` one takes over. With none, each enabled provider
   above is checked, highest first, and the first `ok` one takes over at
   once. The return delay guards a preference while the current provider
   works; it does not hold the observer on one that cannot.
3. **Stuck.** With no other provider answering, the state records
   `stuck: {provider, since}` and notifies once. Later runs log only. The
   flag clears when the current provider stops being reported out, or on any
   switch.
4. **Fail up.** When the current provider is not out, each enabled provider
   above it is checked. `ok` starts or extends its streak in
   `okSince[provider]`. A streak of `RETURN_DELAY` returns the observer to
   that provider. Any other verdict clears the streak.

## Checks

| Provider | Check | `exhausted` | `inactive` |
|---|---|---|---|
| `openrouter` | 1-token chat completion at the gateway per model, up to the first three in `CLAUDE_MEM_OPENROUTER_MODEL` (else `cmem-observer`) | `allowance_exhausted`, else 402 | `key_invalid` or `subscription_inactive`, else 401 |
| `gemini` | 1-token `generateContent` with the configured model, key in `x-goog-api-key` | 429, `RESOURCE_EXHAUSTED`, "quota exceeded" | 401, 403, or 400 naming an invalid key |
| `claude` | none; reads `quota-cooldown.json` | an entry for `claude` armed under 30 minutes ago | — |

A candidate, any provider but the current one, with a `quota-cooldown.json`
entry armed under 30 minutes ago is `exhausted` without a probe: the worker
still refuses it, so a probe that answers would not make it usable. The
current provider's own check still probes, because there the entry is the
signal the probe must confirm.

For the gateway the error code decides before the status. A 403 without a
key code can name a model the key may not use, so the probe tries the
worker's next fallback model. A success with any model is `ok`; the verdict
is `unknown` only when every model tried answers that way (sd:1236).

Everything else is `unknown`, which moves nothing. Any verdict but `ok`
ends that provider's streak, in the down step as well as the up step.

The `claude` row uses the worker's own window. The worker refuses a provider
for 30 minutes after arming its cooldown, then admits one probe. Only a
success with that provider removes the entry, and that never happens while
another provider runs. An older entry is therefore history: the move to
`claude` is allowed, the worker probes it, and a failure re-arms the entry.
The watchdog then sees `claude` out and no other provider answering, and
notifies once.

The Gemini model follows the worker: a model outside its list becomes
`gemini-flash-latest`. The key travels in a header, so it never lands in a
URL or a log line.

## State

`pro-watchdog.json`:

```json
{"provider": "gemini", "since": "...", "okSince": {"openrouter": null},
 "probes": {"openrouter": {"at": "...", "verdict": "exhausted", "detail": "HTTP 402 allowance_exhausted"}},
 "stuck": null, "lastFlip": "...", "lastFlipWhy": "...", "lastRun": "..."}
```

The first shape carried `mode` and a scalar `okSince`. `pro` maps to
`openrouter` and `fallback` to `claude`. The scalar streak becomes
`okSince.openrouter` when the old mode was `fallback`. `lastProbe*` moves under
`probes.openrouter`. Other keys, such as a hand-written `note`, stay.

## Why this shape

- **Down only moves down.** A provider above the current one returns through
  the streak, even when the current one is out. The delay exists because
  neither cmem.ai nor Google publishes a reset time, and one success is weak
  evidence.
- **The default chain is the old toggle.** With `openrouter,claude` the
  checks and the thresholds are the old ones. One difference is deliberate:
  a `claude` cooldown under 30 minutes old now blocks a move from Pro to
  Claude, per R3.
- **Endpoints are overridable.** `CLAUDE_MEM_PRO_GATEWAY` existed;
  `CLAUDE_MEM_GEMINI_ENDPOINT` joins it so the tests use a stub.
