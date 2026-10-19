# local-task-actions

Tiny localhost HTTP service that served the decision links in the Review
Queue digest (`local-obsidian-review`): **Accept**, **Decline** and similar
links that flipped one frontmatter `status` in a vault note. Those decisions
now happen on the dashboard's Tasks page against the workflow database
(sd:5134), and the digest links each card there. The server stays up so a
link in a brief filed before that change answers with a page that says so;
it reads and writes no note.

## How it works

- `task-actions.sh run` serves `http://127.0.0.1:8766` (localhost only,
  threaded). `/task` and `/open`, the old links, answer `410` with a page
  that sends you to Tasks; `/static/<name>` serves the page's files; anything
  else answers `404`.
- A LaunchAgent keeps it running: `start` renders
  `task-actions.plist.template` for this checkout and `$HOME` into
  `~/Library/LaunchAgents/<label>.plist`, label
  `$SYSTEM_TOOLS_LABEL_PREFIX.task-actions` (prefix default
  `local.system-tools`).

## Usage

```
./task-actions.sh start    # install + load the LaunchAgent
./task-actions.sh stop     # unload it
./task-actions.sh status   # agent + HTTP state (exit 0 ok, 1 degraded, 3 not loaded)
./task-actions.sh run      # foreground server (what launchd calls)
```

`status` asks tailscaled where the funnel is, bounded by
`TASK_ACTIONS_FUNNEL_TIMEOUT` (3 seconds by default), and reports
`public: UNKNOWN` when the daemon does not answer in time.

## Caveats

- The server binds 127.0.0.1; **Tailscale Funnel** fronts it publicly so
  an old link answers from any device: `tailscale funnel --bg 8766`. The serve
  config lives inside tailscaled and survives restarts, so there is no extra
  LaunchAgent — but it does **not** survive replacing the daemon, which is
  the one thing that reads as "one-time" and is not. `task-actions.sh status`
  probes the public URL too. Nothing the server answers changes a file.
  The pages wear the ui-design foundation (sd:3591): `/static/<name>` serves
  `tokens.css`, `theme.js`, `fonts.css` and the Plex Sans font from the
  dashboard's `sd_dashboard/v2/static/`, plus this folder's `confirm.css`,
  from a fixed list under a policy that runs only those files. Restart the
  agent (`start`) after pulling a change to the page.
- **Replacing tailscaled can drop the funnel silently.** A daemon that
  starts from an empty state directory (for example after moving tailscale
  from the App Store build to the Homebrew formula) answers
  `tailscale funnel status` with `No serve config`. The local server stays
  healthy, so only old links from another device break. Restore it with
  `tailscale funnel --bg 8766`; `task-actions.sh status` reports
  `public: not configured` until then.
- **Funnel serves locally but external clients get ERR_SSL_PROTOCOL_ERROR**:
  the serve config was registered before Funnel was enabled on the tailnet
  (control then sends the ingress relays without capabilities and the node
  denies them — `peerapi: ingress: denied; no ingress cap` in the logs).
  Fix: `tailscale serve reset && tailscale funnel --bg 8766`, then confirm
  `task-actions.sh status` shows `public: up`. Cost a debugging round on
  2026-08-23.
- Environment overrides, exported or in `<config>/task-actions/.env` (`<config>` is
  `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`; see `.env.example`): `TASK_ACTIONS_PORT` (8766),
  `TASK_ACTIONS_FUNNEL_TIMEOUT` (3). `SYSTEM_TOOLS_LABEL_PREFIX` is kept once in
  `<config>/shared.env`; an exported value wins.
- Tests: `task-actions.sh test` runs `tests/`; `launchctl` and `tailscale` are stubs there.
