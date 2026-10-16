# local-brew-doctor

Nightly Homebrew health check. Runs `brew doctor`, auto-fixes what needs no
human decision, and reports the rest through the run's report.

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
- **Report-only**: deprecated/disabled formulae and casks,
  untrusted taps (trust is a security decision), kegs with no formula, and
  anything else `brew doctor` raises.
- **Ignored**: Homebrew's Intel x86_64 platform warning; it repeats every
  night on an Intel Mac and nothing here can clear it (sd:4785).

The summary goes to the job log with one line per warning, `SD_REPORT_ATTENTION:
brew doctor on <host>: <its first line> [<checksum>]`, and one per item it
lists, `... on <host>: <its first line>: <item>`. The checksum covers the
warning's prose, so the same warnings each night stay one open report, and a
night with fewer warnings or items folds into it too (pack sd:4658). The run's
ingest maps that line to a report that
needs attention, so Reports and Today carry it until it is acknowledged;
nothing is mailed (sd:3454).

## Gotchas

- Clean machine = no attention line, nothing to do, exit 0.
- The job never installs, uninstalls, or trusts anything.
