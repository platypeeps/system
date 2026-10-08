---
paths:
  - "local-jev/**"
  - "local-adversarial-gate/**"
  - "local-drive-intake/**"
  - "local-health-check/**"
  - "local-mail-intake/**"
  - "local-notify/**"
  - "local-obsidian-review/**"
  - "local-obsidian-tasks/**"
  - "local-project-dashboard/**"
  - "local-repo-sync/**"
  - "local-scan-for-secrets/**"
  - "local-sd-db/**"
---

# Jev: switch, exit codes, test pins

- `jev enabled STAGE` exits 0 when Jev can answer and the stage is on, 3 otherwise, and calls nothing.
- `--fallback` prints your answer and exits 0 when Jev is off, unkeyed or failing, with the reason on stderr.
- Unset means on; a stage variable only switches off, and cannot switch on against no key or `jev off`.
- Keep the off-words (`0 off false no disabled`, case-insensitive) in `local-jev/jev.py` only; do not copy them per caller.
- `jev off` writes the kill switch file `~/.config/jev/enabled`; `JEV_ENABLED=0` switches one call off; no `TYPESAFE_API_KEY` equals off.
- An absent or unreadable switch file means enabled.
- Caller rules (local-only callers, `--subject`/`JEV_RUN`, stage name, fallback tokens): `tests/test_jev_contract.py` enforces them; read its failure.
- Hash a content-derived subject key (first 16 hex of sha256); document the key and its recompute in the caller's README.
- `jev.py` redacts credentials and `privacy-patterns` matches before sending; that is a backstop, never a reason to send.
- Keep `jev shadow` off when its file is absent; it sends a real call per decision and changes what callers see.
- A suite that runs a caller end to end switches its stage off; unset reaches the live endpoint.
- Stub `jev enabled STAGE` in suites by reading that stage's variable, as the real command does; a stub that ignores the argument cannot express "switched off".
- A suite that can reach the real `jev` pins `JEV_METER=0`, `JEV_CORPUS=0` or a temp `JEV_CORPUS_DIR`, and `JEV_TRACES_URL=0`; unpinned, it writes the operator's ledger and corpus.
  - Pin the URL to an off word: `jev.sh` refills an unset or empty one from `<config>/jev/.env` (sd:2799).
