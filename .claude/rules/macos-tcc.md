---
paths:
  - "local-project-dashboard/**"
  - "local-cron-jobs/**"
  - "local-msgsnap/**"
  - "local-mirror-sync/**"
  - "local-notify/**"
  - "local-obsidian-review/**"
  - "local-obsidian-tasks/**"
  - "local-task-actions/**"
---

# macOS TCC under launchd


Every LaunchAgent that touches `~/Documents` (the vault, mirror-sync's `~/Documents` pair) is subject to TCC.
Nothing under launchd can click a prompt, so an ungranted read waits instead of failing.

- **TCC attributes a read to the executing binary, not the job's shell.**
- **User FDA grants for Apple platform CLI binaries are not honored under launchd.**
  - Xcode's `/usr/bin/python3` fails as `Python.app`, as the inner binary, toggled, and after `tccutil reset`.
  - Non-Apple binaries (Homebrew `Python.app`, `~/.local/bin/claude`) hold their grants.
- **The vault interpreter is Homebrew python by elimination**, and each binary needs its own grant:
  - `DASHBOARD_PYTHON` in `local-project-dashboard/.env`, for `dashboard.sh tile` and `queue-open` from a shell;
  - `SD_DASHBOARD_PYTHON` in the LaunchAgent, for the server and the tiles it runs as children;
  - PATH `python3` for any other vault reader.
- Do not re-pin to `/usr/bin/python3` for upgrade-proofness; it cannot be granted.
- A `brew upgrade python` moves the Cellar path and silently drops that path's grant.
  - `collectors.vault_blocked()` reports it on the first vault-reading tile asked, naming the interpreter to re-grant.
  - `local-project-dashboard/dashboard.sh grants` probes both paths in one run and names each result.
- **To test a grant, do not run the command from a terminal.** Terminal's own
  FDA leaks in. Bootstrap a one-shot plist under `gui/$(id -u)` and read the
  output file — `dashboard.sh grants` is the command to bootstrap for the
  vault interpreters, and it detects the leak rather than being fooled by it:
  run from a terminal it exits 3 and says its answers are the shell's own
  access, 3 again when its control gave no conclusive result — it never
  started, the call on it failed, or it came back nonzero saying something
  that is not an access refusal — and under launchd it exits 0 when both
  binaries hold the grant, or, and this is the one other way (sd:845), when
  its `/bin/ls` control was still waiting at the timeout, which is what a read
  nobody can grant looks like. Coming back unsettled and running out the clock
  are not one answer: only the second carries the lines above it. Whether the
  deployed shape reaches 0 at all is still unmeasured: it needs one launchd
  run of the verb, and a transient job raises a TCC prompt.
