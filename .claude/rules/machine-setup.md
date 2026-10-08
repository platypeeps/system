---
paths:
  - "local-machine-setup/**"
---

# A stage that can remediate must name what it is remediating

- Print a marker word that the drift grep in `status` counts (read that grep in `machine-setup.sh`); a remediation line alone lets `--fail-on-drift` exit 0.
- Replacing a daemon is not restarting it.
  - Config inside a daemon's own state (a tailscale serve config, anything under `/Library/<vendor>`) vanishes when its provider changes.
  - When a migration swaps a service's provider, enumerate what the service was holding.
- After such a swap on a profile with `<prefix>.task-actions` (prefix: `$SYSTEM_TOOLS_LABEL_PREFIX`), check `tailscale funnel status`; the public URL can be gone while every status line is green.
- `local-machine-setup/profile-autocapture.sh` does not answer `help` inside eight seconds; bound any probe of it.
- Name a cron job in `profiles/<profile>.cron` or put it in `cron-jobs/jobs/<host>/`: the cron stage uninstalls a `cron-jobs.sh install`ed job that neither lists.
- Profiles, dotfiles, envs and LaunchAgent templates live in `$SYSTEM_TOOLS_CONFIG/machine-setup/`; never commit them here, only `examples/`.
- Tests read `tests/fixtures/config`, never the operator's config; set `SYSTEM_TOOLS_CONFIG` in every case that runs the script.
- Call `fixture_config.seal` in a case with a stubs folder: real `defaults` and `sudo` read the Mac whatever `HOME` says.
