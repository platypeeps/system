---
paths:
  - "local-machine-setup/**"
---

# A stage that can remediate must name what it is remediating

Moved from the root `CLAUDE.md`. History: `docs/claude-md-history.md`.

- `machine-setup.sh status` counts drift by grepping stage output for marker words.
  - Markers: `DIFFERS`, `MISSING`, `STALE`, `ABSENT`, `UNLOADED`, `EXTRA`, `defaults write`.
- Print a marker word whenever a stage finds drift; a remediation line alone lets `--fail-on-drift` exit 0.
- Replacing a daemon is not restarting it.
  - Config inside a daemon's own state (a tailscale serve config, anything under `/Library/<vendor>`) vanishes when its provider changes.
  - When a migration swaps a service's provider, enumerate what the service was holding.
- After such a swap on a profile with `<prefix>.task-actions` (prefix: `$SYSTEM_TOOLS_LABEL_PREFIX`), check `tailscale funnel status`; the public URL can be gone while every status line is green.
- `local-machine-setup/profile-autocapture.sh` does not answer `help` inside eight seconds; bound any probe of it.
- Profiles, dotfiles, envs and LaunchAgent templates live in `$SYSTEM_TOOLS_CONFIG/machine-setup/`; never commit them here, only `examples/`.
- Tests read `tests/fixtures/config`, never the operator's config; set `SYSTEM_TOOLS_CONFIG` in every case that runs the script.
