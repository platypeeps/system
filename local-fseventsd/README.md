# local-fseventsd

fseventsd's resident size, and a TERM when an oversized daemon is also hurting
the machine.

On 2026-11-11 fseventsd held 28.77 GB resident on one machine, against Apple's
own footprint budget of about 30 MB, with swap at 19.70 of 20.48 GB and
`kern.memorystatus_vm_pressure_level` at 2 (fleet sd:8557). There is no vendor
fix. launchd respawns the daemon, and System Integrity Protection refuses
`launchctl kickstart` on it, so TERM is the whole repair.

## Usage

```sh
./fseventsd.sh status   # one line, starting with the resident size; exit 0, 3 or 1
./fseventsd.sh check    # one line per reading, then the status line
./fseventsd.sh repair   # the same two answers, and a TERM only when both say yes
./fseventsd.sh test -v  # this folder's portable suite
```

`tests/macos/` holds the three tests that read the live daemon through the real
`pgrep` and `ps`. They need macOS, so they stay out of the portable suite and
`tests/macos-only-suites.txt` names them; run them with
`sh tests/run-macos-only.sh fseventsd-macos`.

`status` exits 3 when there is nothing to check: `FSEVENTSD_RSS_MB` is not set,
this is not macOS, or no fseventsd is running. It exits 1 on the resident size
alone, whatever the machine is doing, and 0 otherwise. local-health-check reads
these codes, so its nightly sweep runs `status` from its own launchd job and
reports only the 1.

The first line always starts with the resident size, so the trend is visible on
a healthy machine too, and the sweep shows that line as the finding's remedy
text.

## Two questions, one line each

Every line answers both questions, and each caller reads the one it needs:

| Question | Reading | Who acts on it |
| --- | --- | --- |
| Is the daemon oversized? | resident size over `FSEVENTSD_RSS_MB` | `status`, in its exit code |
| Is the machine hurting? | `kern.memorystatus_vm_pressure_level` above 1, or `vm.swapusage` used over `FSEVENTSD_SWAP_MB` | `repair`, before it signals |

The two never fold into one answer. An oversized daemon is worth a nightly line
before the machine starts swapping, and the sweep prints nothing for exit 0, so
`status` reports the size on its own. A large fseventsd on a machine with memory
to spare is still doing its job, and the TERM drops the FSEvents history that
every backup and indexing tool reads, so `repair` signals only when both
questions answer yes.

A sysctl that does not answer leaves the second question unknown. Unknown is
neither a yes nor a no: the line says so, and `repair` sends no TERM and exits 1
when the daemon is oversized.

`status` reads the daemon with `pgrep -x fseventsd` and `ps -o rss= -p`, and
asks for no `comm` column: `ps` cuts its output to the window width when it
writes to a terminal, and the daemon's own path is 134 characters, so a name
matched in that field goes missing in a narrow window.

## Configuration

`$SYSTEM_TOOLS_CONFIG/fseventsd/.env`, or exported values (copy
`.env.example`):

| Variable | Meaning |
| --- | --- |
| `FSEVENTSD_RSS_MB` | Resident megabytes the daemon may hold; 4096 in the example. Unset, there is nothing to check. |
| `FSEVENTSD_SWAP_MB` | Swap megabytes in use that counts as hurting, default 8192 — the number local-health-check already uses, not a second one. |
| `FSEVENTSD_MIN_HOURS` | Hours between two TERMs, default 6; 0 for no floor. |

The last TERM's timestamp is state a run writes and nobody edits, so it goes in
`$XDG_STATE_HOME/system/fseventsd/last-term`, not in the config folder, which
holds values only.

## The TERM needs sudo

`repair` signals the daemon with `sudo -A /usr/bin/pkill -TERM -x fseventsd`,
the exact command and arguments that `local-machine-setup/machine-setup-repairs.sudoers.example`
names. pkill by name and not kill by pid, because a pid argument would need a
wildcard in that line, and a wildcard there allows any pid.

Without that sudoers line, sudo asks machine-setup's askpass for the
`machine-setup-sudo` Keychain item (`local-machine-setup/README.md`,
"Unattended sudo"). With neither, sudo refuses, `repair` exits 1 and reports
sudo's own reason.

## Hourly from launchd

`local-cron-jobs/examples/fseventsd-hourly.job` runs `repair` every hour. To
install it on a machine:

```sh
mkdir -p ~/.config/system/fseventsd ~/.config/system/cron-jobs/jobs
cp local-fseventsd/.env.example ~/.config/system/fseventsd/.env
cp local-cron-jobs/examples/fseventsd-hourly.job ~/.config/system/cron-jobs/jobs/
sh local-cron-jobs/cron-jobs.sh install fseventsd-hourly
```

Both copies go to `$SYSTEM_TOOLS_CONFIG` when that is set, or to
`$XDG_CONFIG_HOME/system` when that is (`lib/config.sh`); the lines above are
the default paths, as every sibling README writes them.

`local-machine-setup/examples/profiles/common.cron` lists the job, so
`machine-setup.sh update cron` installs it on every machine whose profile
directory holds that file.

The nightly health check runs the same reading through its sweep; this job only
shortens the wait, and it is the one that can repair. A failed `repair` sends
the cron-jobs failure notification; a run that found nothing to do, or that the
rate floor held back, exits 0.
