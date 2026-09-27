# local-n8n

n8n run via `npx` (Node v22 through fnm, sqlite backend in `~/.n8n`) plus the
external task-runner container on `:5680`, managed as a macOS LaunchAgent
(`$SYSTEM_TOOLS_LABEL_PREFIX.n8n`, prefix default `local.system-tools`) that starts at login, restarts if it dies, and logs to
`./logs/` — same pattern as `local-cswap`. The editor/webhook base URL comes
from `N8N_PUBLIC_HOST` (`.env`); a tunnel must front the webhook path before
use. Only the **webhook path** is funnelled to
the internet — the editor and REST API stay on the tailnet, so the public and
editor base URLs are deliberately different hosts.

## Usage

```sh
cp .env.example .env   # once, fill in encryption key, runner token, public host
./n8n.sh start         # install + load the LaunchAgent
./n8n.sh status        # agent state + log tail
./n8n.sh stop          # unload agent, stop the task-runner container
./n8n.sh run           # one manual foreground run (no launchd)
./n8n.sh update        # refresh the task-runner image (agent must be stopped)
```

`N8N_ENCRYPTION_KEY`, `N8N_RUNNERS_AUTH_TOKEN` and `N8N_PUBLIC_HOST` may
come from `.env` or straight from the environment; `N8N_EDITOR_HOST` and
`N8N_ENDPOINT_WEBHOOK` are optional overrides — `.env` is optional when they are already exported. When both
are present, `.env` wins.

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

- **No basic auth.** `N8N_BASIC_AUTH_*` was removed in n8n 1.x and is ignored
  by the pinned version (0 references in the install tree, vs 4 for
  `N8N_ENCRYPTION_KEY`). What replaces it, all set in `n8n.sh` and all
  verified live in the pinned build:
  `N8N_MFA_ENABLED` (enrol the owner account in the UI — otherwise its
  password really is the only gate), `N8N_PUBLIC_API_DISABLED` (closes
  `/api/v1`), `N8N_SECURE_COOKIE`. Per-webhook Header/Basic auth on the
  webhook nodes themselves authenticates individual callers.
- **Expose the webhook path, not the root.** The public tunnel should map
  only `/webhook`, e.g.
  `tailscale funnel --bg --https=8443 --set-path /webhook https+insecure://localhost:5678/webhook`,
  with the editor reached over the tailnet via plain `tailscale serve`. That
  keeps the UI, `/rest`, and stored credentials off the internet entirely.
  Funnelling `/` instead would publish the whole engine.
- **The n8n version is pinned** in `n8n.sh` to the release that wrote
  `~/.n8n/database.sqlite`, so a normal run migrates nothing. Bumping the pin
  migrates stored workflows and credentials in place — back the DB up first
  (`cp ~/.n8n/database.sqlite ~/.n8n/database.sqlite.bak-$(date +%F)`).
- `N8N_ENCRYPTION_KEY` must never change once workflows store credentials — losing it means losing all encrypted credentials.
- TLS uses a self-signed cert from `~/.ssh/ssl/` (`n8n_self_signed.pem` = crt+key combined, cert block first; `NODE_EXTRA_CA_CERTS` points at the same file). Renew it before it expires.
- `start` renders `n8n.plist.template` (`@LABEL@`, `@DIR@`, `@HOME@`) into `~/Library/LaunchAgents`, so moving the folder only needs a fresh `start`.
- `run` clears any leftover `n8n-task-runner` container before starting and stops it on exit, so n8n and its runner live and die together under KeepAlive.
- `tests/` holds a small webhook test fixture.

## First run — publish and verify

**1. Publish the webhook path** (this is the only step that exposes anything):

```sh
tailscale funnel --bg --https=8443 --set-path /webhook https+insecure://localhost:5678/webhook
echo 'N8N_PUBLIC_HOST=<machine>.<tailnet>.ts.net:8443' >> .env
./n8n.sh start && ./n8n.sh status
```

**2. Verify the split actually held.** The decisive check:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://<machine>.<tailnet>.ts.net:8443/
```

`404` = success, the editor is not public. **`200` means the whole engine is
exposed** — tear the funnel down before doing anything else.

**3. Enrol the owner account in MFA in the UI.** `N8N_MFA_ENABLED` only arms
the feature. Until enrolment, that one password is the only thing in front of
the stored credentials.

### Open questions

- Whether `--set-path` strips the prefix before forwarding. If webhooks 404,
  this is the first knob — try the target with and without the trailing
  `/webhook`.
- Whether 8443 is Funnel-eligible on your tailnet; the command errors
  immediately if not.

### Rollback

Some Tailscale versions have no `funnel ... off`; the documented clear is
`tailscale funnel reset`, **which also drops every other funnel on the
machine**. Record `tailscale funnel status --json` before the change so you
can rebuild the others by hand.

### Loose ends

- An old `./.env` may still carry `N8N_BASIC_AUTH_USER` /
  `N8N_BASIC_AUTH_PASSWORD`. Both are dead (see Gotchas) — prunable any time.
- Serving plain HTTP on loopback is an option, since Funnel terminates TLS
  anyway; that drops the cert dependency for everything except direct tailnet
  access.
