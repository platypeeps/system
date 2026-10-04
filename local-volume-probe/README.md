# local-volume-probe

Bounded reads of configured paths, for a volume that a launchd job can stop
reading without failing.

On 2026-10-01 every launchd job that opened a file on the external volume
waited from 15:50 until 07:31 the next morning (sd:2537). Interactive sessions
kept writing to the same volume, and the jobs failed one at a time as their
own timeouts ran out. This tool reads each configured path in a child process
with a time bound, and names every path whose read is still waiting.

## Usage

```sh
./volume-probe.sh status   # one line; exit 0, 3 or 1
./volume-probe.sh check    # one line per path, then the status line
./volume-probe.sh test -v  # this folder's suite
```

`status` exits 0 when every path answered within the bound. It exits 3 when
`VOLUME_PROBE_PATHS` is not set, and 1 when a path was still waiting at the
bound, is missing, or refused the read. local-health-check reads these codes,
so its nightly sweep runs `status` from its own launchd job.

## Configuration

`$SYSTEM_TOOLS_CONFIG/volume-probe/.env`, or exported values (copy
`.env.example`):

| Variable | Meaning |
| --- | --- |
| `VOLUME_PROBE_PATHS` | Paths to read, separated by `:`. A directory is listed with `ls`; a file has its first byte read with `head`. |
| `VOLUME_PROBE_TIMEOUT` | Whole seconds each read may take, 1 to 60, default 5. |
| `VOLUME_PROBE_READER` | Optional command that reads one path, given as its last argument. Plain words only, no quoting. |

Keep the number of paths times the timeout under the health check's 30-second
status bound (`HEALTH_CHECK_STATUS_BOUND`).

## The answer belongs to the context that runs it

TCC attributes a read to the executing binary, and nothing under launchd can
answer a prompt, so an ungranted read waits instead of failing
(`.claude/rules/macos-tcc.md`). Two consequences:

- Run from a terminal, `status` reports the terminal's access. Only a run
  under launchd answers for the jobs. The health check's sweep is one; the
  hourly job below is the other.
- The default readers are `ls` and `head`. A grant held or lost by one binary,
  such as a Homebrew `Python.app` after `brew upgrade`, needs that binary as
  `VOLUME_PROBE_READER`.

A read still waiting at the bound gets TERM, then KILL. A process that
survives KILL is named with its pid and never waited on, so `status` stays
inside the health check's bound.

## Hourly from launchd

`local-cron-jobs/examples/volume-probe-hourly.job` runs `status` every hour.
To install it on a machine:

```sh
cp local-cron-jobs/examples/volume-probe-hourly.job "${SYSTEM_TOOLS_CONFIG:-$HOME/.config/system}/cron-jobs/jobs/"
sh local-cron-jobs/cron-jobs.sh install volume-probe-hourly
```

A failed run sends the cron-jobs failure notification.
