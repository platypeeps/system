# local-brew-doctor

Nightly Homebrew health check. Runs `brew doctor`, auto-fixes what needs no
human decision, and emails a summary of the rest.

## Usage

```sh
./brew-doctor.sh run
```

Scheduled by `local-cron-jobs/examples/brew-doctor-nightly.job` (02:15); copy it
into `<config>/cron-jobs/jobs/` and run `cron-jobs.sh install brew-doctor-nightly`.

## What it fixes vs reports

- **Auto-fixed**: broken symlinks and stale files (`brew cleanup
  --prune=all`), unlinked kegs that still have a formula (`brew link`,
  never `--overwrite` — an orphaned keg once clobbered a cask binary here).
- **Report-only, emailed**: deprecated/disabled formulae and casks,
  untrusted taps (trust is a security decision), kegs with no formula, and
  anything else `brew doctor` raises.

Email goes through `local-notify`'s `email` channel (Gmail via the local
`google_workspace_mcp` server; `NOTIFY_EMAIL_TO`/`EMAIL_FROM` in
`<config>/notify/.env`, where `<config>` is
`${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}`). If the email cannot be sent the job exits 1, which
triggers `local-cron-jobs`' failure notification (banner + ntfy push), so a
report never disappears silently.

## Gotchas

- Clean machine = no email, nothing to do, exit 0.
- The job never installs, uninstalls, or trusts anything.
