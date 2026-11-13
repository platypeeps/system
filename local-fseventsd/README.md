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

A reading that did not answer at all exits 1, never 3. `pgrep` exits 1 for no
match, and that alone means no fseventsd is running; its 2, its 3 and a missing
`pgrep` are failures to read, and reporting those as nothing to check would
exit 3, which the sweep prints nothing for, so the machine would stop being
checked in silence.

The first line always starts with the resident size, so the trend is visible on
a healthy machine too, and the sweep shows that line as the finding's remedy
text.

A failing `repair` prints one `SD_REPORT_ATTENTION: data safe, one step failed:
...` line. local-cron-jobs reads it and opens a report without pushing "data at
risk" to the phone: no failure here risks data, since the TERM either went out,
which is exit 0, or it did not.

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

Write each value in plain decimal. A leading zero is refused, because shell
arithmetic reads it as octal: `08` is not a number at all there and `010` means
eight.

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

`local-cron-jobs/examples/fseventsd-hourly.job` runs `repair` every hour.
`local-machine-setup/examples/profiles/common.cron` names it, so every machine
installs it through the refresh it already answers (sd:8607): the cron stage
installs the job file from that example, and the envs stage installs
`<config>/fseventsd/.env` from `.env.example` here. One step per machine still
needs a terminal, the sudoers line above.

On a machine that does not run machine-setup, the same two files and the
install by hand:

```sh
mkdir -p ~/.config/system/fseventsd ~/.config/system/cron-jobs/jobs
cp local-fseventsd/.env.example ~/.config/system/fseventsd/.env
cp local-cron-jobs/examples/fseventsd-hourly.job ~/.config/system/cron-jobs/jobs/
sh local-cron-jobs/cron-jobs.sh install fseventsd-hourly
```

Both copies go to `$SYSTEM_TOOLS_CONFIG` when that is set, or to
`$XDG_CONFIG_HOME/system` when that is (`lib/config.sh`); the lines above are
the default paths, as every sibling README writes them.

The nightly health check runs the same reading through its sweep; this job only
shortens the wait, and it is the one that can repair. A failed `repair` opens a
cron-jobs report row and sends no phone push, because every failing path here
grades itself data safe (above); a run that found nothing to do, or that the
rate floor held back, exits 0.

## The repair moves state in steps

`repair` records the rate floor, then signals. Each step's failure, and what
the next run finds:

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 0 | nothing yet | the state path, the clock or the temporary file does not answer | no TERM, exit 1, and the line says which step refused | `test_a_state_folder_that_gives_no_path_refuses_the_term`, `test_a_clock_that_gives_no_timestamp_refuses_the_term`, `test_no_temporary_file_for_sudos_errors_refuses_the_term` |
| 1 | the floor marker gets this run's epoch seconds | the write fails | no TERM, since step 2 never runs, and the floor still holds what it had | `test_the_attempt_is_recorded_before_the_term`, `test_a_floor_whose_folder_cannot_be_made_refuses_the_term`, `test_a_floor_that_cannot_be_written_refuses_the_term`, `test_a_floor_that_cannot_be_written_keeps_what_it_had` |
| 2 | the daemon gets TERM through `sudo -A` | sudo refuses, or nothing matched | step 1 is undone to the value it held, or removed, and the run exits 1 | `test_a_failed_term_restores_the_floor_it_had`, `test_a_failed_first_term_leaves_no_floor_behind` |
| 2 | — | the undo of step 1 fails too | the run exits 1 and the line names the file a hand must remove | `test_an_undo_that_cannot_be_written_names_the_file_to_remove` |
| 2 | — | the process dies between the two steps | the floor is already recorded, so the next hourly run does not signal again | `test_the_attempt_is_recorded_before_the_term` |
| — | the marker holds a value from an older run | it is not a number, or it is in the future | no TERM, exit 1, and the line names the file to remove | `test_a_marker_that_holds_no_timestamp_refuses_and_pages`, `test_a_marker_in_the_future_refuses_and_pages` |
| — | nothing | a sysctl read does not answer | the machine's answer is unknown, no TERM, exit 1 | `test_repair_sends_no_term_on_an_unknown_verdict` |
| — | nothing | `ps` cannot read a pid `pgrep` named | that pid is skipped, never sized at 0 and read as within budget | `test_a_pid_ps_cannot_read_is_skipped_not_sized_at_zero`, `test_a_daemon_only_ps_cannot_read_is_nothing_to_check` |
