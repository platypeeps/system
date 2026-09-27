"""Doubles for the two kinds of provider the registry names.

The registry has exactly two shapes and they fail in different ways, so the
harness doubles both:

* a **`url` entry** -- `kimi`, `minimax`, `baseten`, `exo` -- an
  OpenAI-compatible endpoint the library calls itself. `ProviderDouble`
  answers `POST /v1/chat/completions` and records the request, including the
  token usage a capped bill is charged against. One server serves every named
  provider, routed by a leading path segment, so a test that names three
  providers starts one server and reads one call list in order.

* a **`start` entry** -- `claude`, `codex` -- a command the library spawns.
  `install_start_command` writes that command as a script on a directory the
  test puts first on PATH. The script is the fixture provider: it appends its
  argv, its environment and its stdin to a transcript file, then prints what
  the entry's `reader` expects. A test asserts on the transcript rather than
  on a mock, so the assertion covers the spawn as the library actually
  performs it -- the argument vector, the working directory and the variables
  the entry is allowed to pass.

Both record. Neither guesses: a request for a provider the test did not
register is a 404, and an unregistered path on a registered provider is a 404
as well, so a caller reaching for an endpoint nobody wrote is a failure and
not a silent success.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

#: The registry as it ships, split by shape. Kept here so a test names a
#: provider rather than repeating an endpoint, and so a provider added to
#: `WORKFLOW.md` is added in one place on this side too.
URL_PROVIDERS = ("kimi", "minimax", "baseten", "exo")
START_PROVIDERS = ("claude", "codex")


@dataclass
class ProviderCall:
    """One request a provider endpoint was sent."""

    provider: str
    method: str
    path: str
    body: dict | None = None
    status: int = 200


@dataclass
class Reply:
    """What a provider returns for the next call, and what it costs.

    `status` other than 200 makes the endpoint fail the way a real one does,
    which is the case the retry and the cap both turn on.
    """

    text: str = "ok"
    status: int = 200
    prompt_tokens: int = 100
    completion_tokens: int = 50
    error: str = "provider refused"


class ProviderDouble:
    """One server answering for every `url` provider the test registers.

    `base_url(name)` is what the registry's `url` key is set to. Replies are
    a queue per provider; when it empties the provider's `default` reply is
    used, so a test states only the replies it cares about.
    """

    def __init__(self, providers: tuple[str, ...] = URL_PROVIDERS) -> None:
        self.calls: list[ProviderCall] = []
        self.replies: dict[str, list[Reply]] = {name: [] for name in providers}
        self.default: dict[str, Reply] = {name: Reply() for name in providers}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def base_url(self, name: str) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/{name}/v1"

    def __enter__(self) -> ProviderDouble:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    # ------------------------------------------------------------ replies

    def queue(self, name: str, *replies: Reply) -> None:
        """Answer the next calls to `name` with these, in order."""
        self.replies.setdefault(name, []).extend(replies)

    def calls_to(self, name: str) -> list[ProviderCall]:
        return [call for call in self.calls if call.provider == name]

    def _next_reply(self, name: str) -> Reply:
        queued = self.replies.get(name) or []
        if queued:
            return queued.pop(0)
        return self.default.get(name, Reply())

    # ------------------------------------------------------------ routing

    def answer(self, method: str, path: str, body: dict | None) -> tuple[int, object]:
        parts = [part for part in path.split("/") if part]
        name = parts[0] if parts else ""
        if name not in self.replies:
            # Not a 404 with a recorded provider: nothing named this, so
            # there is no call list it belongs on.
            return 404, {"error": {"message": f"no provider {name!r}"}}
        rest = "/" + "/".join(parts[1:])
        if method != "POST" or rest != "/v1/chat/completions":
            self.calls.append(ProviderCall(name, method, rest, body, 404))
            return 404, {"error": {"message": f"no route for {method} {rest}"}}

        reply = self._next_reply(name)
        self.calls.append(ProviderCall(name, method, rest, body, reply.status))
        if reply.status >= 400:
            return reply.status, {"error": {"message": reply.error}}
        return 200, {
            "id": f"chatcmpl-{len(self.calls)}",
            "object": "chat.completion",
            "model": (body or {}).get("model", "fixture"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": reply.text},
                }
            ],
            "usage": {
                "prompt_tokens": reply.prompt_tokens,
                "completion_tokens": reply.completion_tokens,
                "total_tokens": reply.prompt_tokens + reply.completion_tokens,
            },
        }

    def _handler(self):
        double = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                """Silence. The recorded calls are the log."""

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                status, payload = double.answer(method, urlparse(self.path).path, body)
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

        return Handler


START_SCRIPT = r'''#!/usr/bin/env python3
"""A fixture provider. Written by `sd_db.testing.providers`.

It records the spawn -- argv, cwd, the environment it received and its stdin
-- then prints the transcript the entry's `reader` parses. Recording the
environment is the point: an entry may pass only the variables it names, and
a test can only assert that against what the process actually got.
"""
import json
import os
import stat
import sys


def read_stdin():
    """Read piped input, and never block waiting for input nobody sends.

    `isatty()` alone is not enough: a process spawned with an inherited pipe
    that no one writes to is not a terminal, and `read()` on it blocks until
    the writer closes -- which, for a pipe held by a test runner, is never.
    Confirmed on 2026-09-06: this script hung for nine minutes under the
    suite. Only a regular file or a pipe is read.
    """
    try:
        mode = os.fstat(0).st_mode
    except OSError:
        return ""
    if not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)):
        return ""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


# The name comes from argv[0], not from the environment, so two providers
# installed at once do not overwrite each other's settings.
NAME = os.path.basename(sys.argv[0])
SLOT = NAME.upper().replace("-", "_")
TRANSCRIPT = os.path.join(os.environ["SD_FIXTURE_SPAWNS"], NAME + ".jsonl")
OUTPUT = os.environ.get("SD_FIXTURE_OUTPUT_" + SLOT, "")
STATUS = int(os.environ.get("SD_FIXTURE_STATUS_" + SLOT, "0"))

stdin = read_stdin()

with open(TRANSCRIPT, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "provider": NAME,
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "stdin": stdin,
    }) + "\n")

if STATUS:
    sys.stderr.write(OUTPUT or f"{NAME}: refused\n")
    raise SystemExit(STATUS)

sys.stdout.write(OUTPUT or json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "ok",
    "usage": {"input_tokens": 100, "output_tokens": 50},
}) + "\n")
'''


@dataclass
class StartProvider:
    """A `start` entry the test can spawn and then read back.

    `command` is what the registry's `start` key is set to. `spawns()` is
    what the library actually did with it.
    """

    name: str
    path: Path
    transcript: Path
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def command(self) -> str:
        return str(self.path)

    def spawns(self) -> list[dict]:
        if not self.transcript.exists():
            return []
        return [
            json.loads(line)
            for line in self.transcript.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def install_start_command(
    bin_dir: Path,
    name: str,
    *,
    output: str | None = None,
    status: int = 0,
    transcript: Path | None = None,
) -> StartProvider:
    # `transcript` names the directory the spawns are written into, one file
    # per provider; the script derives its own file from its argv[0].
    """Write `name` onto `bin_dir` as a provider that records its spawn.

    Returns the provider. `environment` on it is what the caller must add to
    the spawned process's environment; the test merges it, so the harness
    never reaches into `os.environ` on the caller's behalf.
    """
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / name
    target.write_text(START_SCRIPT, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    spawns = Path(transcript) if transcript is not None else bin_dir / "spawns"
    spawns.mkdir(parents=True, exist_ok=True)
    log = spawns / f"{name}.jsonl"
    slot = name.upper().replace("-", "_")
    environment = {
        "SD_FIXTURE_SPAWNS": str(log.parent),
        f"SD_FIXTURE_STATUS_{slot}": str(status),
    }
    if output is not None:
        environment[f"SD_FIXTURE_OUTPUT_{slot}"] = output
    return StartProvider(name=name, path=target, transcript=log, environment=environment)


REGISTRY_TEMPLATE = """\
bills:
  anthropic: {{ cost: subscription }}
  openai:    {{ cost: subscription }}
  moonshot:  {{ cost: prepaid }}
  minimax:   {{ cost: plan }}
  baseten:   {{ cost: company, cap_usd_month: 50 }}
  local:     {{ cost: local }}
providers:
  claude:  {{ start: "{claude}", vendor: anthropic, bill: anthropic, roles: [author, reviewer], reader: claude-json }}
  codex:   {{ start: "{codex}", vendor: openai, bill: openai, roles: [author, reviewer], reader: codex-json }}
  kimi:    {{ url: "{kimi}", model: kimi-k3, vendor: moonshot, bill: moonshot, roles: [reviewer], max_tokens: 16384, price: {{ in: 3.00, out: 15.00 }} }}
  minimax: {{ url: "{minimax}", model: MiniMax-M3, vendor: minimax, bill: minimax, roles: [reviewer], max_tokens: 16384, price: {{ in: 0, out: 0 }} }}
  baseten: {{ url: "{baseten}", model: fixture-model, vendor: deepseek, bill: baseten, roles: [reviewer], max_tokens: 16384, price: {{ in: 1.32, out: 3.96 }} }}
  exo:     {{ url: "{exo}", model: fixture-local, vendor: local, bill: local, roles: [author, reviewer], enabled: false, reason: "model not pinned" }}
roles:
  author:   [claude, codex]
  reviewer: [codex, claude, minimax, kimi, baseten, exo]
"""


def write_registry(
    path: Path,
    *,
    double: ProviderDouble,
    start: dict[str, StartProvider] | None = None,
) -> Path:
    """Write a registry whose endpoints and commands are the doubles above.

    The shape is `WORKFLOW.md`'s, with the `url` and `start` values replaced.
    A test that needs a different shape writes its own file; this is the
    common case, not a schema.
    """
    commands = {name: name for name in START_PROVIDERS}
    for name, provider in (start or {}).items():
        commands[name] = provider.command
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        REGISTRY_TEMPLATE.format(
            **commands,
            **{name: double.base_url(name) for name in URL_PROVIDERS},
        ),
        encoding="utf-8",
    )
    return path


def provider_environment(
    bin_dir: Path,
    *providers: StartProvider,
    base: os._Environ | dict | None = None,
) -> dict[str, str]:
    """PATH plus what each `start` provider needs to record its spawn."""
    environment = dict(base if base is not None else os.environ)
    environment["PATH"] = f"{Path(bin_dir)}{os.pathsep}{environment.get('PATH', '')}"
    for provider in providers:
        environment.update(provider.environment)
    return environment
