# local-task-actions

Tiny localhost HTTP service backing the action buttons in the Obsidian task
digest email (`local-obsidian-tasks`): **Mark done** and **Postpone** links
that edit TaskNote frontmatter directly in the vault.

## How it works

- `task-actions.sh run` serves `http://127.0.0.1:8766/task` (localhost only,
  threaded). A LaunchAgent keeps it running: `start` renders
  `task-actions.plist.template` for this checkout and `$HOME` into
  `~/Library/LaunchAgents/<label>.plist`, label
  `$SYSTEM_TOOLS_LABEL_PREFIX.task-actions` (prefix default
  `local.system-tools`).
- Links are HMAC-SHA256-signed with a secret in
  `~/.config/task-actions/secret` (auto-generated, 0600, never committed)
  and expire after 7 days. `task-actions.sh url <file-stem> <done|postpone>
  [days]` prints a signed URL — that's what the digest builder calls.
- `done`: non-recurring tasks get `status: done`; recurring tasks get the
  occurrence appended to `complete_instances` and `scheduled` advanced per
  the recurrence rule (DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL).
- `postpone`: `scheduled` moves N days out from today or the current
  schedule, whichever is later (default 3).

## Usage

```
./task-actions.sh start    # install + load the LaunchAgent
./task-actions.sh stop     # unload it
./task-actions.sh status   # agent + HTTP state (exit 0/1/2)
./task-actions.sh run      # foreground server (what launchd calls)
./task-actions.sh url "My Task" done
./task-actions.sh base-url # the base every signed link is built on
```

## The base URL is discovered once, and a stalled daemon says so

`url` needs a base to sign against, and finding it means asking tailscaled
where the funnel is. That call is bounded by `TASK_ACTIONS_FUNNEL_TIMEOUT`
(3 seconds by default), and its failure is not silent:

| Exit | Means |
| --- | --- |
| `0` | a base URL, public if a funnel proxies this port, `http://127.0.0.1:8766` otherwise |
| `2` | tailscaled did not answer within the timeout |

The distinction is the point. A machine with no funnel is a configured
machine, and a localhost base is the right answer for it. A daemon that will
not answer is a machine that probably does have one, and a localhost link
there opens on this machine and nowhere the mail gets read.

A caller that signs many links in a row asks `base-url` once and passes the
answer back in `TASK_ACTIONS_BASE_URL`; `url` then signs locally and touches
no daemon. `local-obsidian-review` does this, and it is not an optimisation:
before it did, a sixty-card digest paid the funnel timeout on each of the
three or four buttons per card -- around ten minutes of waiting -- and signed
every one of them against 127.0.0.1 (sd:1203).

## Caveats

- The server binds 127.0.0.1; **Tailscale Funnel** fronts it publicly so
  the buttons work from any device: `tailscale funnel --bg 8766`. The serve
  config lives inside tailscaled and survives restarts, so there is no extra
  LaunchAgent — but it does **not** survive replacing the daemon, which is
  the one thing that reads as "one-time" and is not. `task-actions.sh status`
  probes the public URL too. GET never mutates (confirmation page + POST),
  links are HMAC-signed and expire, everything else 404s.
- **Replacing tailscaled can drop the funnel silently.** A daemon that
  starts from an empty state directory (for example after moving tailscale
  from the App Store build to the Homebrew formula) answers
  `tailscale funnel status` with `No serve config`. The local server stays
  healthy, so only the digest buttons break. Restore it with
  `tailscale funnel --bg 8766`; `task-actions.sh status` reports
  `public: not configured` until then.
- **Funnel serves locally but external clients get ERR_SSL_PROTOCOL_ERROR**:
  the serve config was registered before Funnel was enabled on the tailnet
  (control then sends the ingress relays without capabilities and the node
  denies them — `peerapi: ingress: denied; no ingress cap` in the logs).
  Fix: `tailscale serve reset && tailscale funnel --bg 8766`, then confirm
  `task-actions.sh status` shows `public: up`. Cost a debugging round on
  2026-08-23.
- **macOS TCC**: the vault lives in `~/Documents`, so the launchd-spawned
  python needs a one-time grant — answer the "Python would like to access
  files in your Documents folder" prompt, or add python3 under System
  Settings > Privacy & Security > Files and Folders / Full Disk Access.
  Until granted, action requests block and return nothing.
- Environment overrides, exported or in `<config>/task-actions/.env` (`<config>` is
  `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`; see `.env.example`): `TASK_ACTIONS_PORT` (8766), `OBSIDIAN_VAULT` (default
  `~/Documents/Obsidian Vault`), `OBSIDIAN_TASKS_SUBDIR`,
  `TASK_ACTIONS_SECRET_FILE`, `SYSTEM_TOOLS_LABEL_PREFIX`.
