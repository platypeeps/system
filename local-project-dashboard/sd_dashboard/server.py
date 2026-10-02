"""The server: explicit listeners and an authentication policy on every response.

Task controls use an expiring browser session bound to its origin and verified
operator. Remote requests require the explicitly configured private Tailscale
Serve boundary to be revalidated on every request; headers cannot select it.

* **Every** response carries
  `Content-Security-Policy: default-src 'self'; script-src 'self';
  object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors
  'none'` and `X-Frame-Options: DENY` -- error pages, the 404, the stylesheet
  and the JavaScript file included. It is set in one place, `end_headers`, so
  a route added later cannot forget it.
* **No page may be framed**, which is the last directive above and the older
  header beside it. That is not a content rule: it is what stops a page on
  another origin of the same tailnet site from putting an overlay over a real
  control (`prd.md:706-716`).
* The primary socket binds loopback for Tailscale Serve. Optional direct IP
  access binds one validated Tailscale address and authenticates its socket peer.

Connections come from `sd_db.connect`: reads open `write=False`, actions open
a writer only after the request and finite action vocabulary are validated.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import sd_db
from sd_db import workflow
from sd_db.errors import SdDbError
from sd_db.database import schema_version
from sd_db.schema import SCHEMA_VERSION

from . import auth, runtime, screens
from .pages import error_page

__all__ = ["CSP", "Dashboard", "SECURITY_HEADERS", "build", "capture_followup", "main", "route"]

CSP = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

#: On every response. The tuple is what the test enumerates.
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)

STATIC = Path(__file__).resolve().parent / "static"

#: The one CSS file and the one JavaScript file, and no third. Criterion 12
#: reads this tuple and the directory listing and asserts they agree.
STATIC_FILES = ("dashboard.css", "dashboard.js")

DEFAULT_PORT = 8767
SESSION_SECONDS = 3600
MAX_BODY = 65536


class NotFound(Exception):
    """A path with no screen behind it."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def route(connection: sqlite3.Connection, path: str, parameters, *, now: str,
          operations_backend=None, services_backend=None, ports_backend=None) -> str:
    """One request, one page. The router is a function so a test can call it.

    A test that has to start a socket to assert a page is a test that runs
    once and then flakes. Everything above `Dashboard` is pure: rows in,
    markup out.
    """
    from . import v2

    # The new design is the default (sd:2163): `/` and `/today` are its Today, a static page whose rows come from
    # /api/now. The old Today moved to /classic/today; every other old screen keeps its path until its section is
    # ported. `v2.CLASSIC` maps each unported rail section to its old screen.
    found = v2.page(path)
    if found is not None:
        return found
    if path == "/classic/today":
        return screens.today(connection, now=now, parameters=parameters)
    if path == "/backlog":
        return screens.backlog(connection, now=now, parameters=parameters)
    if path == "/contributions":
        from .contribution_screen import render

        return render(connection, parameters=parameters)
    if path == "/protection":
        from .protection_screen import render

        return render(connection, parameters=parameters)
    if path == "/writing":
        return screens.writing_page(connection, now=now, parameters=parameters)
    if path == "/skills":
        from .skills_screen import render

        return render(connection, now=now, parameters=parameters)
    if path == "/documents":
        from .documents import render

        # No connection: the listing reads a directory, not the database.
        return render(parameters)
    if path == "/designs":
        from .designs import render as designs_render

        # No connection either: the listing reads the ui-design checkout.
        return designs_render(parameters)
    if path == "/operations":
        from .operations_screen import operations_page

        return operations_page(connection, parameters=parameters, now=now, backend=operations_backend,
                               services_backend=services_backend, ports_backend=ports_backend)
    if path.startswith("/item/"):
        tail = path[len("/item/") :].strip("/")
        if not tail.isdigit():
            raise NotFound(f"no item {tail!r}")
        rendered = screens.item(connection, int(tail), now=now, parameters=parameters)
        if rendered is None:
            raise NotFound(f"no item {tail}")
        return rendered
    raise NotFound(path)


def capture_followup(connection, *, followup_of=None, who, **values):
    """A followup item, filed as `sd task add --kind followup` files one (sd:719 step 6a).

    The pack's verb (the `_capture` function of its `bin/sd_work.py` at a7572f14) is
    `workflow.capture_task`, which hard-codes `kind='task'`, and then the
    library's own `writes.set_item_fields(kind=...)`, both inside one
    `transaction` so nothing observes the row while it still reads `task`.
    The same two calls here, for the same reason: `capture_task` is the
    capture entry point and this is not the place to widen it.

    `followup_of` is the item the form had selected, and it is optional: a
    followup item stands on its own (that is what makes it an item and not a
    note, `local-sd-db/README.md`). Given, the link is written twice, once
    each way: the child carries `fields.followup_of`, and the parent gets a
    `comment` note naming the child -- a `comment`, never a `followup` note,
    which would put the item on Today's open followups as a note as well. The
    child takes the parent's repository unless the form named one, as the
    verb takes the enclosing checkout. Named means a repository: a `repo`
    of `null` is a form that named none, the way a serialised optional
    field arrives, and it inherits like a missing key does (PR #428
    review). The form itself never sends `null`; the JSON route is open to
    any caller.
    """
    from sd_db import writes

    with workflow.transaction(connection):
        parent = workflow.item_state(connection, followup_of)["item"] if followup_of else None
        if parent is not None and values.get("repo") is None:
            values["repo"] = parent["repo"]
        state = workflow.capture_task(connection, who=who, **values)
        item = state["item"]["id"]
        fields = {"fields": {"followup_of": followup_of}} if followup_of else {}
        writes.set_item_fields(connection, item, kind="followup", **fields)
        if parent is not None:
            workflow.add_item_note(connection, followup_of, kind="comment", who=who,
                                   body=f"Followup item #{item} filed: {state['item']['title']}")
        return workflow.item_state(connection, item)


def action_route(path, payload, *, principal, operations_backend=None, services_backend=None, runner_backend=None):
    """Parse one finite action vocabulary before opening a writable connection.

    `principal` is what the listener authenticated for this request
    (`auth.Context.principal`): `local` on loopback, the operator login
    behind Tailscale. Required, with no default, for the same reason the
    library's `who` has none: an action that records it must be handed it,
    not fall back to a name that fits every caller (sd:755).
    """
    values = dict(payload)
    if path == "/api/contributions/acknowledge":
        from sd_db import contributions

        if (set(values) != {"key", "revision", "event_ids"}
                or not isinstance(values["key"], str) or not isinstance(values["revision"], str)
                or not isinstance(values["event_ids"], list) or not values["event_ids"]
                or any(not isinstance(event, str) for event in values["event_ids"])):
            raise ValueError("Provide the contribution key, current revision and exact event IDs.")
        return lambda connection: contributions.acknowledge(connection, values["key"], values["event_ids"],
            expected_revision=values["revision"], who="dashboard")
    if path == "/api/palette/prepare":
        from sd_db import runner_exec

        if set(values) - {"item", "command", "values", "revision", "catalog", "screen", "target"} or not {"item", "command", "values", "revision", "catalog", "screen"} <= set(values):
            raise ValueError("Choose a registered command, item and current revisions.")
        if type(values["item"]) is not int or not 1 <= values["item"] <= 9223372036854775807 or not isinstance(values["command"], str) or not isinstance(values["screen"], str):
            raise ValueError("Choose a valid item and registered command.")
        return lambda connection: runner_exec.prepare(connection, values["item"], values["command"], values["values"],
            expected_revision=values["revision"], expected_catalog=values["catalog"], screen=values["screen"],
            target=values.get("target"), who="dashboard")
    match = re.fullmatch(r"/api/palette/([1-9][0-9]{0,18})/(execute|reconcile)", path)
    if match:
        from sd_db import runner_exec

        if values or int(match[1]) > 9223372036854775807:
            raise ValueError("Execute the recorded command without additional arguments.")
        operation = runner_exec.execute_immediate if match[2] == "execute" else runner_exec.reconcile
        return lambda connection: operation(connection, int(match[1]), backend=runner_backend)
    if path == "/api/providers/configure":
        from sd_db import provider_controls

        if set(values) != {"revision", "enabled", "orders"}:
            raise ValueError("Provide the complete provider configuration and current revision.")
        return lambda connection: provider_controls.configure(connection, enabled=values["enabled"],
            orders=values["orders"], expected_revision=values["revision"], who="dashboard")
    from .operations_screen import BILL_NAME

    match = re.fullmatch(f"/api/bills/({BILL_NAME})/cap", path)
    if match:
        from sd_db import provider_controls

        # Slice 12b (sd:234). The number is `set_cap`'s to judge: it refuses a
        # stale revision (`StaleItem`, 409 below), an unknown bill and a cap on a
        # bill a `start` entry is billed to (`WorkflowError`, `RegistryError`)
        # and a number the ledger would not hold (`LedgerRefused`), the last
        # three `SdDbError`s the `do_POST` mapping already answers with 400
        # and the library's own sentence. `null` clears the cap.
        if set(values) != {"revision", "cap_usd_month"}:
            raise ValueError("Provide the new monthly cap, or null to clear it, and the current revision.")
        return lambda connection: provider_controls.set_cap(connection, match[1], values["cap_usd_month"],
            expected_revision=values["revision"], who="dashboard")
    if path == "/api/run":
        from sd_db import runner_controls

        if set(values) - {"items", "revisions", "parallel", "budget_minutes", "budget_usd", "skill", "skill_revision"} or not {"items", "revisions"} <= set(values):
            raise ValueError("Provide selected items and their current revisions.")
        return lambda connection: runner_controls.enqueue(connection, who="dashboard", **values)
    match = re.fullmatch(r"/api/runner/([1-9][0-9]*)/(cancel|requeue|resume|restore)", path)
    if match:
        from sd_db import runner, runner_controls

        allowed = {"revision", "destination"} if match[2] == "restore" else {"revision"}
        if set(values) != allowed:
            raise ValueError("Provide the current assignment revision and required destination.")
        if match[2] == "requeue":
            return lambda connection: runner.requeue(connection, int(match[1]), expected_revision=values["revision"], who="dashboard")
        return lambda connection: runner_controls.control(connection, int(match[1]), match[2],
            expected_revision=values["revision"], destination=values.get("destination"), who="dashboard", backend=runner_backend)
    match = re.fullmatch(r"/api/items/([1-9][0-9]*)/prepare", path)
    if match:
        from sd_db import runner_controls

        if set(values) != {"revision", "repo", "branch"}:
            raise ValueError("Provide the repository, branch and current revision.")
        return lambda connection: runner_controls.configure_item(connection, int(match[1]), repo=values["repo"],
            branch=values["branch"], expected_revision=values["revision"], who="dashboard")
    match = re.fullmatch(r"/api/skills/(sd-[a-z0-9-]+)/(try|review|promote|demote)", path)
    if match:
        from sd_db import skills_catalog

        if set(values) - {"revision", "path_name"} or "revision" not in values:
            raise ValueError("Provide the current skill revision and optional workflow path.")
        if match[2] == "try":
            return lambda connection: skills_catalog.trial(connection, match[1], expected_revision=values["revision"])
        return lambda connection: skills_catalog.request(connection, match[1], match[2],
            expected_revision=values["revision"], path_name=values.get("path_name"), who="dashboard")
    match = re.fullmatch(r"/api/skill-reviews/([1-9][0-9]*)/apply", path)
    if match:
        from sd_db import skills_catalog

        if set(values) != {"revision", "notes"}:
            raise ValueError("Select proposals and provide the current review revision.")
        return lambda connection: skills_catalog.apply_proposals(connection, int(match[1]), values["notes"], expected_revision=values["revision"], who="dashboard")
    match = re.fullmatch(r"/api/reports/([1-9][0-9]*)/acknowledge", path)
    if match:
        from sd_db import reporting

        if set(values) != {"revision"}:
            raise ValueError("Provide the current report revision.")
        return lambda connection: reporting.acknowledge(connection, int(match[1]), expected_revision=values["revision"], who="dashboard")
    if path == "/api/reports/acknowledge-clean":
        from sd_db import reporting

        # sd:755. The plan the preview issued, the stamped cutoff it was
        # issued at, and the person who is acknowledging: the library refuses
        # a plan that no longer matches, so a body that passes here can
        # still write nothing. `cutoff` runs inside the returned function
        # and not here: it raises `WorkflowError`, which the `try` around
        # this call does not catch, and which `do_POST` maps to 400 only
        # once the action runs.
        if (set(values) != {"before", "plan", "who"}
                or not all(isinstance(values[key], str) for key in values)
                or not re.fullmatch(r"[a-f0-9]{64}", values["plan"])):
            raise ValueError("Preview the clean reports again, and name who is acknowledging them.")
        return lambda connection: reporting.acknowledge_clean(connection,
            before=reporting.cutoff(values["before"]), expected_plan=values["plan"],
            who=values["who"], principal=principal, program="dashboard")
    service_action = re.fullmatch(r"/api/services/([A-Za-z0-9][A-Za-z0-9._-]{0,199})/(start|stop|restart)", path)
    if service_action:
        from sd_db import services

        label, action = service_action.groups()
        if (set(values) != {"revision"} or not isinstance(values["revision"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", values["revision"])):
            raise ValueError("The current service revision is required.")
        mutation = {"start": services.start_service, "stop": services.stop_service,
                    "restart": services.restart_service}[action]
        return lambda connection: mutation(connection, label, backend=services_backend,
            expected_revision=values["revision"], who="dashboard")
    match = re.fullmatch(r"/api/repos/(runner-merge|managed)", path)
    if match:
        from .management_screen import set_repo

        # The two sd-db repo verbs Management runs (sd:2118); `before` is what the page showed, refused when stale.
        words = ("manual", "auto") if match[1] == "runner-merge" else ("yes", "no")
        if (set(values) != {"path", "value", "before"} or not isinstance(values["path"], str)
                or values["value"] not in words or values["before"] not in words or values["value"] == values["before"]):
            raise ValueError(f"Provide the repository path, the new {match[1]} value and the value the page showed.")
        return lambda connection: set_repo(connection, match[1], values["path"], values["value"], values["before"])
    operation = re.fullmatch(r"/api/(jobs|assignments)/([a-z0-9][a-z0-9_-]{0,99})/(retry|cancel)", path)
    if operation:
        from sd_db import operations

        entity, identifier, action = operation.groups()
        if set(values) != {"revision"} or not isinstance(values["revision"], str):
            raise ValueError("The current operation revision is required.")
        options = {"expected_revision": values["revision"], "who": "dashboard"}
        if entity == "assignments":
            if action != "cancel" or not re.fullmatch(r"[1-9][0-9]*", identifier):
                raise NotFound(path)
            return lambda connection: operations.cancel_assignment(connection, int(identifier), **options)
        mutation = operations.retry_job if action == "retry" else operations.cancel_job
        return lambda connection: mutation(connection, identifier, backend=operations_backend, **options)
    if path == "/api/items":
        if set(values) - {"title", "body", "priority", "due", "repo", "kind", "followup_of"}:
            raise ValueError("Unknown task field.")
        if "title" not in values:
            raise ValueError("A title is required.")
        kind = values.pop("kind", "task")
        parent = values.pop("followup_of", None)
        if kind == "task":
            if parent is not None:
                raise ValueError("Only a followup item names the item it follows up.")
            return lambda connection: workflow.capture_task(connection, who="dashboard", **values)
        if kind != "followup":
            raise ValueError("Capture files a task or a followup item.")
        if parent is not None and (type(parent) is not int or not 1 <= parent <= 9223372036854775807):
            raise ValueError("Choose a valid item for the followup to follow up.")
        return lambda connection: capture_followup(connection, followup_of=parent, who="dashboard", **values)
    match = re.fullmatch(r"/api/(items|notes)/([1-9][0-9]*)(/status|/notes|/resolve|/relink|/cancel|/stage|/park|/revive)?", path)
    if not match:
        raise NotFound(path)
    entity, number, suffix = match.groups()
    revision = values.pop("revision", None)
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ValueError("The current item revision is required. Reload the item.")
    options = {"who": "dashboard", "expected_revision": revision}
    number = int(number)
    if entity == "items" and suffix is None:
        return lambda connection: workflow.edit_item(connection, number, values, **options)
    if entity == "items" and suffix == "/status":
        if set(values) - {"status", "reason"} or "status" not in values:
            raise ValueError("A status and optional reason are required.")
        return lambda connection: workflow.change_status(connection, number,
            values["status"], reason=values.get("reason") or None, **options)
    if entity == "items" and suffix == "/notes":
        if set(values) - {"body", "kind"} or "body" not in values:
            raise ValueError("A note body and optional kind are required.")
        return lambda connection: workflow.add_item_note(connection, number, **values, **options)
    if entity == "notes" and suffix == "/resolve" and not values:
        return lambda connection: workflow.resolve_item_note(connection, number, **options)
    if entity == "items" and suffix in ("/park", "/revive"):
        from sd_db import writing

        if values:
            raise ValueError("Parking accepts only the current item revision.")
        return lambda connection: writing.park_piece(connection, number,
            parked=suffix == "/park", actor="human", require_row=True, **options)
    if entity == "items" and suffix == "/stage":
        from sd_db import writing

        if set(values) - {"stage", "reason", "correct"} or "stage" not in values:
            raise ValueError("Provide a stage, optional correction and reason.")
        if "correct" in values and type(values["correct"]) is not bool:
            raise ValueError("Correction must be true or false.")
        if values["stage"] == "published":
            raise ValueError("Publication uses the reviewed publishing workflow.")
        return lambda connection: writing.change_stage(connection, number, values["stage"],
            actor="human", require_row=True, reason=values.get("reason") or None,
            correct=values.get("correct", False), **options)
    if entity == "items" and suffix in ("/relink", "/cancel"):
        from sd_db import progress

        if suffix == "/relink" and set(values) == {"path"}:
            return lambda connection: progress.relink_artifact(connection, number, values["path"], **options)
        if suffix == "/cancel" and set(values) == {"reason"}:
            return lambda connection: progress.cancel_work(connection, number, reason=values["reason"], **options)
        raise ValueError("Provide the artifact path or cancellation reason.")
    raise NotFound(path)


class Dashboard(BaseHTTPRequestHandler):
    """Same-origin task controls over the shared workflow library."""

    server_version = "sd-dashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    #: Set by `build`.
    database: Path | None = None
    clock = staticmethod(_now)
    session_secret: bytes
    operations_backend = None
    services_backend = None
    ports_backend = None
    runner_backend = None
    #: `now_screen.document`'s fleet seam, `area -> document`; None runs the child.
    fleet_backend = None
    frontdoor: auth.FrontDoor | None = None
    frontdoor_check = None
    direct_listener = False
    peer_lookup = None

    # `_policy` lets one response replace the shared Content-Security-Policy,
    # and only that header. A served document carries its stylesheet in its own
    # head, which `default-src 'self'` refuses; the policy it carries instead
    # permits that and forbids script outright, which the shared one does not.
    # The substitution happens here rather than in the route, so the loop below
    # stays the single place any response gets its headers.
    #
    # `_overrides` does the same for any other header in the tuple, by name. A
    # design asset uses it to relax Cross-Origin-Resource-Policy, because the
    # sandboxed mockup that loads it has an opaque origin (see designs.py). An
    # override the tuple does not name is added after it: a design font sends
    # Access-Control-Allow-Origin that way.
    def end_headers(self) -> None:
        policy = getattr(self, "_policy", None)
        overrides = getattr(self, "_overrides", None) or {}
        for name, value in SECURITY_HEADERS:
            if policy and name == "Content-Security-Policy":
                value = policy
            value = overrides.get(name, value)
            self.send_header(name, value)
        named = {name for name, _ in SECURITY_HEADERS}
        for name, value in overrides.items():
            if name not in named:
                self.send_header(name, value)
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - base class
        return

    def handle(self) -> None:
        """A client that left before the response is one line, not a traceback.

        A browser tab navigating away, a curl with `--max-time`, a health
        probe: the write into the closed socket is ordinary, and the stdlib's
        traceback for it was 109 blocks of the err log on one day (sd:970).
        The handler is shared by both listeners, so the direct IP socket is
        covered too; every other exception keeps the stdlib's traceback.
        """
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            host, port = self.client_address[:2]
            print(f"client {host}:{port} left before the response", file=sys.stderr)

    def _send(self, status: int, body: bytes, content_type: str, *, cookie=None,
              policy: str | None = None, overrides: dict | None = None) -> None:
        # Assigned unconditionally: the handler instance is reused for every
        # request on a kept-alive connection, and a policy left behind would
        # apply to the next page served down the same socket.
        self._policy = policy
        self._overrides = overrides
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802 - the base class names it
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        context = self._context()
        if context is None:
            return self._json(403, {"error": "Untrusted dashboard host."})
        split = urlsplit(self.path)
        path = split.path
        if path == "/health":
            return self._health()
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if path.startswith("/ui/"):
            return self._v2_asset(path[len("/ui/") :])
        if path.startswith("/v2/"):
            return self._moved(path, split.query)
        if path.startswith("/documents/"):
            return self._document(path[len("/documents/") :])
        if path.startswith("/designs/"):
            return self._design(path[len("/designs/") :])
        parameters = parse_qs(split.query, keep_blank_values=True)
        try:
            connection = sd_db.connect(self.database, write=False)
        except (FileNotFoundError, SdDbError, sqlite3.Error) as problem:
            return self._send(
                503, error_page(503, str(problem)).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        try:
            if path.startswith("/api/"):
                if path == "/api/contributions":
                    from sd_db import contributions

                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading contribution details."})
                    if split.query:
                        return self._json(400, {"error": "Contribution projection does not accept query parameters."})
                    return self._json(200, {"contributions": contributions.projection(connection)})
                if path == "/api/palette" or re.fullmatch(r"/api/executions/[1-9][0-9]{0,18}", path):
                    from sd_db import runner_exec

                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading command details."})
                    try:
                        if path == "/api/palette":
                            if set(parameters) - {"screen", "item"} or any(len(values) != 1 for values in parameters.values()):
                                raise ValueError("Choose one screen and item.")
                            item = parameters.get("item", [None])[0]
                            if item is not None and (not re.fullmatch(r"[1-9][0-9]{0,18}", item) or int(item) > 9223372036854775807):
                                raise ValueError("Invalid item.")
                            return self._json(200, runner_exec.inventory(connection, screen=parameters.get("screen", ["item"])[0], item=int(item) if item else None))
                        if set(parameters) - {"offset"} or any(len(values) != 1 for values in parameters.values()):
                            raise ValueError("Provide one output offset.")
                        offset = parameters.get("offset", ["0"])[0]
                        if not re.fullmatch(r"[0-9]{1,10}", offset):
                            raise ValueError("Invalid output offset.")
                        return self._json(200, runner_exec.read_execution(connection, int(path.rsplit("/", 1)[1]), offset=int(offset)))
                    except (ValueError, SdDbError) as error:
                        return self._json(400, {"error": str(error)})
                if path == "/api/now":
                    from . import now_screen

                    # The fleet child, the shadow read and the launchd jobs, for
                    # a page that was opened first: the document describes this machine.
                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading Now."})
                    if split.query:
                        return self._json(400, {"error": "Now does not accept query parameters."})
                    return self._json(200, now_screen.document(connection, now=self.clock(), fleet=self.fleet_backend,
                                                           jobs=self.operations_backend))
                if path == "/api/health":
                    from . import health_screen

                    # The Health page's areas (sd:2115): worktrees, missing trailers, ports and branch protection.
                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading Health."})
                    if split.query:
                        return self._json(400, {"error": "Health does not accept query parameters."})
                    return self._json(200, health_screen.document(connection, now=self.clock(), fleet=self.fleet_backend,
                                                                  ports=self.ports_backend))
                if path == "/api/tasks" or re.fullmatch(r"/api/tasks/[1-9][0-9]{0,18}", path):
                    from . import tasks_screen

                    # The Tasks page's rows and one item's Details (sd:2124), read as v1 /backlog and /item/<id> read them.
                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading Tasks."})
                    if split.query:
                        return self._json(400, {"error": "Tasks does not accept query parameters."})
                    if path == "/api/tasks":
                        return self._json(200, tasks_screen.document(connection, now=self.clock()))
                    number = int(path.rsplit("/", 1)[1])
                    if number > 9223372036854775807:
                        return self._json(404, {"error": "No such item."})
                    return self._json(200, tasks_screen.details(connection, number, now=self.clock()))
                if path == "/api/management":
                    from . import management_screen

                    # The Management page's one reading (sd:2118): v1's repo table, fleet, runner, services and jobs reads.
                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading Management."})
                    if split.query:
                        return self._json(400, {"error": "Management does not accept query parameters."})
                    return self._json(200, management_screen.document(connection, now=self.clock(), fleet=self.fleet_backend,
                                                                      jobs=self.operations_backend,
                                                                      services=self.services_backend))
                if path == "/api/home":
                    from . import home_screen

                    # The Home page's tiles (sd:2117), from the config folder; no Home Assistant state is read yet.
                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading Home."})
                    if split.query:
                        return self._json(400, {"error": "Home does not accept query parameters."})
                    return self._json(200, home_screen.document(now=self.clock()))
                if path in ("/api/reports", "/api/reports/clean"):
                    from . import reports_screen

                    # The Reports page's reading and its clean preview (sd:2121): v1's report, job and preview reads.
                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading Reports."})
                    if path == "/api/reports":
                        if split.query:
                            return self._json(400, {"error": "Reports does not accept query parameters."})
                        return self._json(200, reports_screen.document(connection, now=self.clock(),
                                                                       jobs=self.operations_backend))
                    if set(parameters) != {"before"} or len(parameters["before"]) != 1:
                        return self._json(400, {"error": "Give one date: /api/reports/clean?before=YYYY-MM-DD."})
                    try:
                        return self._json(200, reports_screen.clean(connection, parameters["before"][0], now=self.clock()))
                    except workflow.WorkflowError as problem:
                        return self._json(400, {"error": str(problem)})
                if path == "/api/usage":
                    from .usage_screen import document

                    # The read `sd usage --json` prints, as the same bytes.
                    if not self._session(context):
                        return self._json(403, {"error": "Open a dashboard page before reading Usage."})
                    if set(parameters) - {"month"} or any(len(values) != 1 for values in parameters.values()):
                        return self._json(400, {"error": "Usage takes one month, YYYY-MM."})
                    try:
                        body = document(connection, now=self.clock(), month=parameters.get("month", [None])[0])
                    except SdDbError as error:
                        return self._json(400, {"error": str(error)})
                    return self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
                match = re.fullmatch(r"/api/items/([1-9][0-9]{0,18})/capture-context", path)
                if not match or split.query or int(match[1]) > 9223372036854775807:
                    return self._json(404, {"error": "No capture context at this address."})
                state = workflow.item_state(connection, int(match[1]))
                # Only the selected identity and its revision belong in the
                # picker response. Full note history stays on the item page.
                return self._json(200, {
                    "item": {key: state["item"][key]
                             for key in ("id", "title", "kind", "status", "repo", "parked_at")},
                    "revision": state["revision"],
                })
            body = route(connection, path, parameters, now=self.clock(), operations_backend=self.operations_backend,
                         services_backend=self.services_backend, ports_backend=self.ports_backend)
        except workflow.MissingItem:
            return self._json(404, {"error": "The related item no longer exists. Choose another item."})
        except NotFound as missing:
            return self._send(
                404, error_page(404, f"Nothing at {missing}.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        except (SdDbError, sqlite3.Error) as problem:
            return self._send(503, error_page(503, str(problem)).encode("utf-8"),
                              "text/html; charset=utf-8")
        finally:
            connection.close()
        session = self._session(context) or self._new_session(context)
        token = self._csrf(session)
        body = body.replace("</head>", f'<meta name="sd-csrf" content="{token}"></head>', 1)
        cookie = f"sd_session={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
        if context.secure:
            cookie += "; Secure"
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8", cookie=cookie)

    def _context(self):
        try:
            if self.direct_listener:
                context = auth.direct_context(self.headers, self.client_address,
                                              self.frontdoor, self.peer_lookup)
            else:
                context = auth.access_context(self.headers, self.client_address[0],
                                              self.server.server_address[1], self.frontdoor)
            if context and context.remote:
                if self.frontdoor_check() is not True:
                    return None
        except (runtime.RuntimeRefused, OSError, ValueError):
            return None
        return context

    def _health(self):
        """Read-only local health used by the installed runtime's check."""
        try:
            connection = sd_db.connect(self.database, write=False)
            try:
                version = schema_version(connection)
            finally:
                connection.close()
            current_build = runtime.build_digests()
        except (FileNotFoundError, SdDbError, sqlite3.Error, runtime.RuntimeRefused) as problem:
            return self._json(503, {"service": "sd-dashboard", "ok": False, "error": str(problem)})
        changed = current_build != self.loaded_build
        owner = getattr(self.server, "runtime_owner", self.server)
        direct_ok = owner.direct_listener_healthy()
        healthy = version == SCHEMA_VERSION and not changed and direct_ok
        result = {
            "service": "sd-dashboard", "ok": healthy, "code_changed": changed,
            "pid": os.getpid(),
            "schema": version, "expected_schema": SCHEMA_VERSION,
            "library": str(Path(sd_db.__file__).resolve()),
            **self.loaded_build,
        }
        if self.frontdoor is not None and self.frontdoor.direct is not None:
            result["direct_listener_ok"] = direct_ok
            if direct_ok:
                result["ip_origin"] = self.frontdoor.direct.origin
        self._json(200 if healthy else 503, result)

    def _mac(self, value):
        return hmac.new(self.session_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _new_session(self, context):
        value = f"{int(time.time())}.{secrets.token_hex(24)}"
        signature = self._mac(f"{value}\n{context.scope}")
        return f"{value}.{signature}"

    def _session(self, context):
        if context is None or len(self.headers.get_all("Cookie", [])) != 1:
            return None
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            session = cookie["sd_session"].value
            stamp, random, signature = session.split(".")
            if not re.fullmatch(r"[0-9]{1,12}", stamp):
                return None
            age = time.time() - int(stamp)
            if (not (0 <= age <= SESSION_SECONDS)
                    or not re.fullmatch(r"[a-f0-9]{48}", random)
                    or not re.fullmatch(r"[a-f0-9]{64}", signature)):
                return None
            if not hmac.compare_digest(signature, self._mac(f"{stamp}.{random}\n{context.scope}")):
                return None
            return session
        except (CookieError, KeyError, ValueError):
            return None

    def _csrf(self, session):
        return self._mac("csrf." + session)

    def _json(self, status, value):
        # sd:874. `value` is usually a `workflow.item_state` readback, which
        # carries a BLOB column back as `bytes`. The same encoding the revision
        # hash uses, so a row that renders on GET also answers on POST.
        self._send(status, json.dumps(value, default=workflow.json_safe).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        # Failed preflight must not leave unread bytes as another keep-alive request.
        self.close_connection = True
        context = self._context()
        origin = context.origin if context else None
        session = self._session(context)
        csrf = self.headers.get("X-SD-CSRF", "")
        if (context is None or self.headers.get_all("Origin", []) != [origin]
                or self.headers.get_all("Sec-Fetch-Site", ["same-origin"]) != ["same-origin"]
                or len(self.headers.get_all("X-SD-CSRF", [])) != 1
                or not session or not re.fullmatch(r"[a-f0-9]{64}", csrf)
                or not hmac.compare_digest(csrf, self._csrf(session))):
            return self._json(403, {"error": "Open the dashboard again before saving. The browser session, identity or origin is invalid."})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "Expected application/json."})
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isdigit()
                or not 0 < int(lengths[0]) <= MAX_BODY or self.headers.get("Transfer-Encoding")):
            return self._json(400, {"error": "A bounded request body is required."})
        try:
            payload = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
            path = urlsplit(self.path)
            if path.query or path.fragment:
                raise ValueError("Action URLs do not accept query parameters.")
            action = action_route(path.path, payload, principal=context.principal,
                                 operations_backend=self.operations_backend,
                                 services_backend=self.services_backend, runner_backend=self.runner_backend)
        except (ValueError, UnicodeDecodeError) as problem:
            return self._json(400, {"error": str(problem)})
        except NotFound:
            return self._json(404, {"error": "No such action."})
        try:
            connection = sd_db.connect(self.database)
            try:
                from sd_db import reporting
                before = connection.total_changes
                result = action(connection)
                if connection.total_changes > before:
                    reporting.observed_action(connection, path.path)
            finally:
                connection.close()
        except (workflow.MissingItem, workflow.MissingNote) as problem:
            return self._json(404, {"error": str(problem)})
        except workflow.StaleItem as problem:
            return self._json(409, {"error": str(problem), "reload": True})
        except (workflow.WorkflowError, SdDbError, ValueError) as problem:
            return self._json(400, {"error": str(problem)})
        except (FileNotFoundError, sqlite3.Error) as problem:
            return self._json(503, {"error": str(problem)})
        self._json(201 if path.path == "/api/items" else 200, result)

    def _document(self, tail: str) -> None:
        """One generated report, handed over whole.

        Read from disk and written to the socket unchanged. Embedding it in a
        dashboard page would put it through the markup filter, which would
        take its stylesheet and its chart with it, so the filter is avoided
        rather than fought: the document gets its own address and its own,
        tighter policy.
        """
        from .documents import POLICY, resolve

        key, _, name = tail.partition("/")
        target = resolve(key, name)
        if target is None:
            # One answer for every way of missing. Telling a prober which of
            # "no such root" and "outside the root" they reached is telling
            # them how the check works.
            return self._send(
                404, error_page(404, "No such document.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        try:
            body = target.read_bytes()
        except OSError as problem:
            return self._send(
                503, error_page(503, str(problem)).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        self._send(200, body, "text/html; charset=utf-8", policy=POLICY)

    def _design(self, tail: str) -> None:
        """One file from the ui-design checkout, at its path in the tree.

        A page gets the sandboxing policy; an asset keeps the dashboard's policy
        and relaxes only its resource policy, so the sandboxed page may load it.
        """
        from .designs import ASSET_HEADERS, FONT_HEADERS, POLICY, read, resolve

        found = resolve(tail)
        if found is None:
            return self._send(
                404, error_page(404, "No such design file.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        target, kind = found
        try:
            body = read(target)
        except OSError as problem:
            return self._send(
                503, error_page(503, str(problem)).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        if kind.startswith("text/html"):
            return self._send(200, body, kind, policy=POLICY)
        self._send(200, body, kind, overrides=FONT_HEADERS if kind.startswith("font/") else ASSET_HEADERS)

    def _v2_asset(self, name: str) -> None:
        from .v2 import asset

        found = asset(name)
        if found is None:
            return self._send(404, error_page(404, "No such file.").encode("utf-8"), "text/html; charset=utf-8")
        self._send(200, *found)

    def _moved(self, path: str, query: str) -> None:
        """A `/v2/` bookmark: 301 to the page's or asset's address since sd:2163, or the 404 of a path that names neither."""
        from .v2 import moved

        target = moved(path)
        if target is None:
            return self._send(404, error_page(404, "No such page.").encode("utf-8"), "text/html; charset=utf-8")
        location = f"{target}?{query}" if query else target
        self._send(301, b"", "text/plain; charset=utf-8", overrides={"Location": location})

    def _static(self, name: str) -> None:
        if name not in STATIC_FILES:
            return self._send(
                404, error_page(404, "No such file.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        target = STATIC / name
        guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), f"{guessed}; charset=utf-8")


class Listener(ThreadingHTTPServer):
    """A listening socket whose accept queue holds a page's burst of connections (sd:2131).

    socketserver's default backlog is 5. Tailscale serve takes a page's
    assets over one HTTP/2 connection and dials a backend connection for each
    at once; macOS resets a connect beyond the queue, and the proxy answers 502.
    """

    request_queue_size = socket.SOMAXCONN


class DashboardServer(Listener):
    """Own both listeners so partial startup and shutdown leave no orphan socket."""

    direct_server = None
    direct_thread = None

    def direct_listener_healthy(self):
        return self.direct_server is None or (
            self.direct_thread is not None and self.direct_thread.is_alive())

    def _close_direct(self):
        if self.direct_server is not None:
            if self.direct_thread is not None and self.direct_thread.is_alive():
                self.direct_server.shutdown()
                self.direct_thread.join()
            self.direct_server.server_close()

    def serve_forever(self, poll_interval=0.5):
        try:
            if self.direct_server is not None:
                self.direct_thread = threading.Thread(
                    target=self.direct_server.serve_forever, daemon=True,
                    name="sd-dashboard-ip")
                self.direct_thread.start()
                if not self.direct_thread.is_alive():
                    raise RuntimeError("The required dashboard IP listener did not start.")
            super().serve_forever(poll_interval=poll_interval)
        except BaseException:
            self.server_close()
            raise
        finally:
            self._close_direct()

    def server_close(self):
        try:
            self._close_direct()
        finally:
            super().server_close()


def build(database: Path | str | None = None, *, port: int = DEFAULT_PORT,
          host: str = "127.0.0.1", operations_backend=None,
          frontdoor: auth.FrontDoor | None = None, frontdoor_check=None,
          services_backend=None, ports_backend=None, runner_backend=None,
          peer_lookup=None, fleet_backend=None) -> ThreadingHTTPServer:
    """Build the loopback server and optional, separately authenticated IP socket."""
    if host != "127.0.0.1":
        raise ValueError("The dashboard binds 127.0.0.1 only.")
    if frontdoor is not None and not callable(frontdoor_check):
        raise ValueError("Configured remote access requires an ongoing private Serve check.")
    if frontdoor is not None and frontdoor.direct is not None:
        if peer_lookup is None:
            peer_lookup = runtime.peer_login
        if not callable(peer_lookup):
            raise ValueError("Direct access requires a current Tailscale peer lookup.")
    handler = type("BoundDashboard", (Dashboard,), {
        "database": database, "session_secret": secrets.token_bytes(32),
        "operations_backend": operations_backend,
        "services_backend": services_backend,
        "ports_backend": ports_backend,
        "runner_backend": staticmethod(runner_backend) if runner_backend else None,
        "fleet_backend": staticmethod(fleet_backend) if fleet_backend else None,
        "frontdoor": frontdoor,
        "frontdoor_check": staticmethod(frontdoor_check) if frontdoor_check else None,
        "peer_lookup": staticmethod(peer_lookup) if peer_lookup else None,
        "loaded_build": runtime.build_digests(),
    })
    primary = DashboardServer((host, port), handler)
    try:
        if frontdoor is not None and frontdoor.direct is not None:
            direct = frontdoor.direct
            direct_handler = type("DirectDashboard", (handler,), {"direct_listener": True})
            primary.direct_server = Listener((direct.address, direct.port), direct_handler)
            primary.direct_server.runtime_owner = primary
        return primary
    except BaseException:
        primary.server_close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The local workflow dashboard.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=None)
    parser.add_argument("--config", type=Path, default=None)
    arguments = parser.parse_args(argv)
    frontdoor = None
    try:
        if arguments.config:
            config = runtime.read_config(arguments.config)
            if config["port"] != arguments.port:
                raise runtime.RuntimeRefused("Configured backend port differs from --port.")
            if arguments.database is None:
                arguments.database = config.get("database")
        runtime.installed_library(arguments.database)
        if arguments.config:
            frontdoor = runtime.load_frontdoor(arguments.config, arguments.port)
    except (runtime.RuntimeRefused, OSError, ValueError) as problem:
        parser.error(str(problem))
    options = {}
    if frontdoor is not None:
        def private_frontdoor_unchanged():
            return runtime.load_frontdoor(arguments.config, arguments.port) == frontdoor

        options = {"frontdoor": frontdoor, "frontdoor_check": private_frontdoor_unchanged}
    server = build(arguments.database, port=arguments.port, **options)
    host, port = server.server_address[:2]
    print(f"sd-dashboard on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the entrypoint
    raise SystemExit(main())
