# local-fseventsd

fseventsd's resident size, and a TERM when the daemon is both oversized and
hurting the machine.

On 2026-11-11 fseventsd held 28.77 GB resident on one machine, against Apple's
own footprint budget of about 30 MB, with swap at 19.70 of 20.48 GB and
`kern.memorystatus_vm_pressure_level` at 2 (fleet sd:8557). There is no vendor
fix. launchd respawns the daemon, and System Integrity Protection refuses
`launchctl kickstart` on it, so TERM is the whole repair.

## Usage

```sh
./fseventsd.sh status   # one line, starting with the resident size; exit 0, 3 or 1
./fseventsd.sh check    # one line per reading, then the status line
./fseventsd.sh repair   # the same verdict, and a TERM when the machine is hurting
./fseventsd.sh test -v  # this folder's suite
```

`status` exits 3 when there is nothing to check: `FSEVENTSD_RSS_MB` is not set,
this is not macOS, or no fseventsd is running. It exits 1 when the daemon is
over `FSEVENTSD_RSS_MB` and the machine is hurting with it, and 0 otherwise.
local-health-check reads these codes, so its nightly sweep runs `status` from
its own launchd job and reports only the 1.

The first line always starts with the resident size, so the trend is visible on
a healthy machine too, and the sweep shows that line as the finding's remedy
text.

## Two conditions, never one

A large fseventsd on a machine with memory to spare is doing its job, and the
TERM drops the FSEvents history that every backup and indexing tool reads. So
nothing is signalled until both hold:

| Condition | Reading |
| --- | --- |
| The daemon is oversized | resident size over `FSEVENTSD_RSS_MB` |
| The machine is hurting | `kern.memorystatus_vm_pressure_level` above 1, or `vm.swapusage` used over `FSEVENTSD_SWAP_MB` |

A sysctl that does not answer leaves the second question unknown. Unknown is
neither a yes nor a no: `status` says so and exits 1, and `repair` sends no
TERM.

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
cp local-fseventsd/.env.example "${SYSTEM_TOOLS_CONFIG:-$HOME/.config/system}/fseventsd/.env"
cp local-cron-jobs/examples/fseventsd-hourly.job "${SYSTEM_TOOLS_CONFIG:-$HOME/.config/system}/cron-jobs/jobs/"
sh local-cron-jobs/cron-jobs.sh install fseventsd-hourly
```

`local-machine-setup/examples/profiles/common.cron` lists the job, so
`machine-setup.sh update cron` installs it on every machine whose profile
directory holds that file.

The nightly health check runs the same reading through its sweep; this job only
shortens the wait, and it is the one that can repair. A failed `repair` sends
the cron-jobs failure notification; a run that found nothing to do, or that the
rate floor held back, exits 0.
