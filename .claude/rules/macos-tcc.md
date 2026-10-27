---
paths:
  - "local-cron-jobs/**"
  - "local-msgsnap/**"
  - "local-mirror-sync/**"
  - "local-notify/**"
  - "local-obsidian-review/**"
  - "local-kev/**"
  - "local-cswap/**"
  - "local-maintenance/**"
  - "local-machine-setup/examples/launchagents/**"
---

# macOS TCC under launchd

Every LaunchAgent that touches `~/Documents` (the vault, mirror-sync's `~/Documents` pair) is subject to TCC.
Nothing under launchd can click a prompt, so an ungranted read waits instead of failing.

- **TCC attributes a read to the executing binary, not the job's shell.**
- **User FDA grants for Apple platform CLI binaries are not honored under launchd.**
  - Xcode's `/usr/bin/python3` fails as `Python.app`, as the inner binary, toggled, and after `tccutil reset`.
  - Non-Apple binaries (Homebrew `Python.app`, the `claude-code@latest` cask's `/opt/homebrew/bin/claude`) hold their grants.
    - The cask links to a versioned Caskroom binary; moving from the native `~/.local/bin/claude` to it, or a cask upgrade, needs a fresh grant for the new binary.
- A LaunchAgent template starts `/bin/bash -c '"$0" ARGS; exit $?' SCRIPT`, so Apple's bash is the process charged, not a Homebrew binary that loses its grant on upgrade (sd:6427).
  - `local-machine-setup/tests/test_launchagent_launchers.py` fails a template that starts or execs into another program.
- **The vault interpreter is Homebrew python by elimination**, and each binary needs its own grant:
  - `DASHBOARD_PYTHON` in `<config>/project-dashboard/.env`, for `dashboard.sh tile` from a shell;
  - `SD_DASHBOARD_PYTHON` in the LaunchAgent, for the server and the tiles it runs as children;
  - PATH `python3` for any other vault reader.
- Do not re-pin to `/usr/bin/python3` for upgrade-proofness; it cannot be granted.
- Keep every vault reader on one Homebrew Python version: each `Python.app` has bundle id `org.python.python`, and TCC keeps one Full Disk Access row for it, so granting one version revokes the other (sd:5047).
- A `brew upgrade python` moves the Cellar path and silently drops that path's grant.
  - `collectors.vault_blocked()` reports it on the first vault-reading tile asked, naming the interpreter to re-grant.
  - The pack's `dashboard/dashboard.sh grants` probes both paths in one run and names each result.
- **Test a grant under launchd, never from a terminal**: Terminal's own FDA leaks in.
  Bootstrap `dashboard.sh grants` as a one-shot plist under `gui/$(id -u)` and read its output file; `dashboard.sh help` gives its exit codes.
