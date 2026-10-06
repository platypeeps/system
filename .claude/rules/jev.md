---
paths:
  - "local-jev/**"
  - "local-adversarial-gate/**"
  - "local-health-check/**"
  - "local-notify/**"
  - "local-obsidian-review/**"
  - "local-obsidian-tasks/**"
  - "local-sd-db/**"
  - "local-sd-plan/**"
---

# Nothing may depend on Jev: detail


- Stub `jev enabled STAGE` in test suites by reading that stage's variable, as the real command does.
  - A stub that ignores the argument cannot express "switched off".
- Keep the off-words (`0 off false no disabled`, case-insensitive) in `local-jev/jev.py` only; do not copy them per caller.
- `jev off` is a file because cron and launchd read no shell profile.
- An absent switch file means enabled; a default-off switch makes every later integration silently never run.
- A stage variable cannot switch a stage on against a machine with no key or with `jev off`.
- `sd-docs-lint` is a Jev caller outside this repo, opt-in per repo through a tracked `.github/sd-docs-lint.json`.
- `local-scan-for-secrets` calls only through `--local-only` (local Kev); its hits are candidate credentials, so never a hosted model.
- `jev.py` redacts credentials and `privacy-patterns` matches before sending; that is a backstop, never a reason to send.
- Keep `jev shadow` off when its file is absent; it sends a real call per decision and changes what callers see.
- A suite that can reach the real `jev` pins `JEV_METER=0`, `JEV_CORPUS=0` or a temp `JEV_CORPUS_DIR`, and `JEV_TRACES_URL=0`; unpinned, it writes the operator's ledger and corpus.
  - Pin the URL to an off word: `jev.sh` refills an unset or empty one from `<config>/jev/.env` (sd:2799).
