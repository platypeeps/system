"""Credential presence and expiry, for Health's Credentials area (sd:2203).

`sd-db.sh credentials` runs nightly (`local-cron-jobs/examples/
credentials-nightly.job`) in the environment cron gives a job, which has
sourced `~/.config/shell/env.sh`. It writes one `heartbeat` state row under
`HEARTBEAT_KEY`; the dashboard reads the latest and calls nothing (the
operator's ruling of 2026-10-03: no per-request GitHub call or env.sh read).

**A value never leaves its probe.** A token is read from the environment,
put in one request header, and dropped. Nothing here writes, prints, logs or
hashes it: the row holds presence, what a status code means, an expiry date,
MCP server names and states, and reasons written here -- never a response
body, a tool's output, or an error's own words.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .writes import record_state

HEARTBEAT_KEY = "credentials:nightly"
#: Seconds per probe; `claude mcp list` starts every server it checks.
TIMEOUT = 20
MCP_TIMEOUT = 120
GITHUB_USER = "https://api.github.com/user"
#: `claude mcp list`: `<name>: <command or url> - ✓ Connected`. The target is dropped.
_MCP_LINE = re.compile(r"^(?P<name>\S.*?): .* - (?P<mark>[✓✗!⚠])\s*(?P<text>.*)$")
_MCP_STATES = {"✓": "connected", "✗": "failed"}


class _NoRedirect(HTTPRedirectHandler):
    """Follows no redirect: urllib would resend the Authorization header to wherever a 3xx points."""

    def redirect_request(self, *args, **kwargs):
        return None


def _get(url: str, token: str) -> tuple[int, dict[str, str]]:
    """The status and lower-cased headers of a GET carrying `token`; never the body. A redirect is its 3xx status."""
    request = Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "sd-db-credentials"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=TIMEOUT) as reply:  # nosec B310 - fixed or operator-set origin
            return reply.status, {key.lower(): value for key, value in reply.headers.items()}
    except HTTPError as error:
        return error.code, {}


def _run(argv: list[str], timeout: int) -> tuple[int, str]:
    """Exit code and stdout; stderr is dropped unread, since `gh` names its token source there."""
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, timeout=timeout, check=False)
    return done.returncode, done.stdout


def _expiry(value: str | None) -> str | None:
    """GitHub's `2026-12-01 00:00:00 UTC` as ISO UTC, or None."""
    if not value:
        return None
    try:
        moment = datetime.strptime(value.strip().replace(" UTC", " +0000"), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_probe(probe: dict[str, Any], token: str | None, url: str | None, get) -> dict[str, Any]:
    """`present`, then `valid` from one GET: True on 200, False on 401 or 403, else a reason."""
    probe["present"] = bool(token)
    if not token or not url:
        return probe
    try:
        status, headers = get(url, token)
    except (OSError, ValueError) as error:
        probe["reason"] = f"not reached: {type(error).__name__}"
        return probe
    probe["valid"] = True if status == 200 else False if status in (401, 403) else None
    if probe["valid"] is None:
        probe["reason"] = f"answered HTTP {status}"
    probe["headers"] = headers
    return probe


def github_pat(env, get) -> dict[str, Any]:
    probe = _token_probe({"id": "github_pat", "name": "GitHub PAT (GITHUB_PERSONAL_ACCESS_TOKEN)"},
                         env.get("GITHUB_PERSONAL_ACCESS_TOKEN"), GITHUB_USER, get)
    headers = probe.pop("headers", {})
    if probe.get("valid"):
        raw = headers.get("github-authentication-token-expiration")
        probe["expires"] = _expiry(raw)
        if raw and probe["expires"] is None:
            probe["reason"] = "expiry header not understood"
    return probe


def ha_token(env, get) -> dict[str, Any]:
    url = (env.get("HA_URL") or "").rstrip("/")
    probe = _token_probe({"id": "ha_token", "name": "Home Assistant token (HA_TOKEN)"},
                         env.get("HA_TOKEN"), f"{url}/api/" if url else None, get)
    probe.pop("headers", None)
    if probe["present"] and not url:
        probe["reason"] = "HA_URL unset, so the token was not tested"
    return probe


def gh_cli(run) -> dict[str, Any]:
    probe: dict[str, Any] = {"id": "gh", "name": "gh CLI sign-in (gh auth status)"}
    try:
        code, _ = run(["gh", "auth", "status", "--hostname", "github.com"], TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        probe["reason"] = f"gh auth status did not run: {type(error).__name__}"
        return probe
    probe["signed_in"] = code == 0
    return probe


def mcp_servers(run) -> dict[str, Any]:
    probe: dict[str, Any] = {"id": "mcp", "name": "MCP servers (claude mcp list)"}
    try:
        code, out = run(["claude", "mcp", "list"], MCP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        probe["reason"] = f"claude mcp list did not run: {type(error).__name__}"
        return probe
    servers = []
    for line in out.splitlines():
        match = _MCP_LINE.match(line.strip())
        if match:
            text = match["text"].lower()
            state = _MCP_STATES.get(match["mark"]) or ("needs_auth" if "auth" in text else "other")
            servers.append({"name": match["name"], "status": state})
    if code != 0 or not servers:
        probe["reason"] = f"claude mcp list exited {code}" if code else "claude mcp list named no server"
        return probe
    probe["servers"] = servers
    return probe


def check(connection, *, env, get=None, run=None, now: str | None = None) -> dict[str, Any]:
    """Run every probe and record the heartbeat; the body it wrote.

    `get` and `run` default to `_get` and `_run`, looked up at call time so a test can replace them."""
    get, run = get or _get, run or _run
    body = {"probes": [github_pat(env, get), gh_cli(run), ha_token(env, get), mcp_servers(run)]}
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record_state(connection, "heartbeat", key=HEARTBEAT_KEY, timestamp=stamp, body=body)
    return body


def read(connection) -> tuple[str, dict[str, Any]] | None:
    """The latest heartbeat as `(timestamp, body)`, or None; a body with no probe list is a ValueError."""
    row = connection.execute("SELECT timestamp, body FROM state WHERE kind = 'heartbeat' AND key = ? "
                             "ORDER BY id DESC LIMIT 1", (HEARTBEAT_KEY,)).fetchone()
    if row is None:
        return None
    body = json.loads(row["body"] or "{}")
    if not isinstance(body, dict) or not isinstance(body.get("probes"), list):
        raise ValueError("the credentials heartbeat has no probe list")
    return row["timestamp"], body


def describe(probe: dict[str, Any]) -> str:
    """One line per probe for the verb's output: facts, no value."""
    facts = [f"{key} {probe[key]}" for key in ("present", "valid", "signed_in", "expires") if key in probe]
    if "servers" in probe:
        facts.append(f"{sum(s['status'] == 'connected' for s in probe['servers'])} of "
                     f"{len(probe['servers'])} servers connected")
    if probe.get("reason"):
        facts.append(probe["reason"])
    return f"{probe['id']}: {', '.join(facts) or 'nothing read'}"
