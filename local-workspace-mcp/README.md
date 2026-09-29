# local-workspace-mcp

Health probe for the host `google_workspace_mcp` server. The server is not in
this repo: it runs from `~/repos/ai/google_workspace_mcp` under the
`$SYSTEM_TOOLS_LABEL_PREFIX.google-workspace-mcp` LaunchAgent on
`127.0.0.1:8083` (prefix default `local.system-tools`; `WORKSPACE_MCP_LABEL`
overrides the whole label). This folder
only watches it.

## Why

The server can stop answering (`curl` 000; its `service.err.log` holds
`RuntimeError: No response returned.` from the session middleware) while its
process stays alive. `KeepAlive` restarts only a process that exits, so
nothing restarts it, and every client loses Gmail and Drive until a hand
`launchctl kickstart -k`. The server needs
about 90s to answer after a restart.

## Usage

```sh
./workspace-mcp.sh status   # 0 healthy, 3 nothing to check, 1 up and broken
./workspace-mcp.sh watch    # status; on 1, restart once and notify if still down
./workspace-mcp.sh test -v  # unittest suite against stub launchctl, curl, notify
```

`status` POSTs one MCP `initialize` and wants HTTP 200 with an
`mcp-session-id` header. It then sends `notifications/initialized` on that
session, as every MCP client must, and closes the session with `DELETE`, so
repeated probes leave no sessions behind. All three calls share one 10s
deadline (`WORKSPACE_MCP_TIMEOUT`). The close is never skipped: past the
deadline it still gets 3s (`WORKSPACE_MCP_CLOSE_TIMEOUT`), because a slow
server is exactly the one a skipped close leaks into, one session per probe.
The probe ends within 13s, inside `local-health-check`'s 30s bound. A close
that fails (a curl error, or HTTP other than 2xx or 405) is broken, since each
probe would leave a session behind. `watch` reports a failed close but never
restarts for it: the server is alive. It answers 3 when the LaunchAgent is not loaded, so a
machine without the server stays silent, and so
does the window where `local-maintenance` boots the agent out for a uv prune.
`local-health-check` sweeps it. `status` never restarts anything.

`watch` is what the `workspace-mcp-watch` cron job runs every 10 minutes. On a
1 it keeps probing for up to 90s first: a server launchd is still starting
needs about that long, and a restart then would reset its startup and drop
every live session. An answer in that window ends the run with no restart.
When the whole window fails but the agent's process is younger than 300s
(`WORKSPACE_MCP_STARTUP`), the run exits 1 without a restart: it is still
starting, and under heavy load that took minutes on 2026-09-28. A later run
restarts it if it stays down. Only an older agent gets `launchctl kickstart -k` once and
poll `status` for up to 150s. When the agent is booted out during either
wait, the run ends with exit 0 and neither restarts nor notifies. When
the server still does not answer, the first run of the outage notifies through
`local-notify` with `-c local,ntfy -p high`, and the job exits 1. Email is not
among the channels: `notify.sh` sends mail through this same server. The
notifier runs under a 30s watchdog (`WORKSPACE_MCP_NOTIFY_TIMEOUT`), because its
ntfy push has no deadline of its own. A stalled notifier and its children are
killed, and the next run tries again. Later runs
of the same outage restart again without a second notice; the cron wrapper
still reports each exit 1. A healthy probe clears the marker, so the next
outage notifies again. The marker lives in `~/.local/state/workspace-mcp/`.

`help` lists the environment overrides (URL, label, timeout, close timeout,
startup age, wait, poll, state directory, notifier).

## Install

Install the cron job on the machine that runs the server:

```sh
../local-cron-jobs/cron-jobs.sh install workspace-mcp-watch
```
