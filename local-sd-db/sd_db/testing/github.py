"""Two doors onto one `FixtureRemote`, and every call through them recorded.

The pack shells out to `gh`; the runner reaches the REST API. An HTTP double
alone would miss every `gh` invocation, and a `gh` stub alone would miss the
REST calls, so this module provides both and points them at the same object.
A call arriving either way is answered from one table and recorded once, with
the door it came through.

The REST surface is exactly what the pack and the runner ask for and nothing
else, enumerated from the callers rather than from the API:

    GET    /repos/{slug}                                repo, default branch
    GET    /repos/{slug}/branches                       the branch list
    GET    /repos/{slug}/branches/{branch}/protection   the protection state
    GET    /repos/{slug}/collaborators                  who else can push
    GET    /repos/{slug}/compare/{base}...{head}        behind_by
    GET    /repos/{slug}/pulls                          the open pull requests
    GET    /repos/{slug}/pulls/{n}                      state, mergeable, sha
    GET    /repos/{slug}/commits/{ref}/check-runs       the checks
    PUT    /repos/{slug}/pulls/{n}/merge                merge, with `sha`
    DELETE /repos/{slug}/git/refs/heads/{branch}        the branch deletion

An unknown path is a 404 and is recorded as one. A double that quietly
answers a call nobody wrote is a double that hides a caller.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .remote import FixtureRemote, RemoteRefusal

COMPARE = re.compile(r"^/repos/(?P<slug>[^/]+/[^/]+)/compare/(?P<base>.+?)\.\.\.(?P<head>.+)$")
PULL = re.compile(r"^/repos/(?P<slug>[^/]+/[^/]+)/pulls/(?P<number>\d+)$")
MERGE = re.compile(r"^/repos/(?P<slug>[^/]+/[^/]+)/pulls/(?P<number>\d+)/merge$")
PROTECTION = re.compile(r"^/repos/(?P<slug>[^/]+/[^/]+)/branches/(?P<branch>.+)/protection$")
REF = re.compile(r"^/repos/(?P<slug>[^/]+/[^/]+)/git/refs/heads/(?P<branch>.+)$")
CHECKS = re.compile(r"^/repos/(?P<slug>[^/]+/[^/]+)/commits/(?P<ref>[^/]+)/check-runs$")


def _pull_json(remote: FixtureRemote, pull) -> dict:
    return {
        "number": pull.number,
        "title": pull.title,
        "body": pull.body,
        "state": pull.state.lower(),
        "draft": pull.draft,
        "mergeable": pull.mergeable == "MERGEABLE",
        "mergeable_state": pull.merge_state_status.lower(),
        "merge_commit_sha": pull.merge_commit_sha,
        "head": {"ref": pull.head, "sha": pull.head_sha(remote)},
        "base": {"ref": pull.base},
    }


class GitHubDouble:
    """An HTTP server answering the calls above from one `FixtureRemote`.

    Started and stopped by the test. `base_url` is what a caller points at;
    `remote.calls` is what it asked for.
    """

    def __init__(self, remote: FixtureRemote) -> None:
        self.remote = remote
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> GitHubDouble:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    # ------------------------------------------------------------ routing

    def answer(self, method: str, path: str, body: dict | None, door: str = "http") -> tuple[int, object]:
        """The whole routing table. Returns `(status, payload)`."""
        try:
            status, payload = self._route(method, path, body)
        except RemoteRefusal as refusal:
            self.remote.record(door, method, path, body, refusal.status)
            return refusal.status, {"message": refusal.message}
        self.remote.record(door, method, path, body, status)
        return status, payload

    def _route(self, method: str, path: str, body: dict | None) -> tuple[int, object]:
        remote = self.remote
        slug = remote.slug

        if method == "GET" and path == f"/repos/{slug}":
            return 200, {
                "full_name": slug,
                "default_branch": remote.default_branch,
                "fork": False,
                "private": True,
                "delete_branch_on_merge": remote.delete_branch_on_merge,
                "permissions": {"admin": True, "push": True},
            }

        if method == "GET" and path == f"/repos/{slug}/branches":
            return 200, [{"name": name} for name in remote.branches()]

        if method == "GET" and path == f"/repos/{slug}/collaborators":
            return 200, list(remote.collaborators)

        if method == "GET" and path == f"/repos/{slug}/pulls":
            return 200, [
                _pull_json(remote, pull)
                for pull in remote.pull_requests.values()
                if pull.state == "OPEN"
            ]

        match = PROTECTION.match(path)
        if match and method == "GET" and match["slug"] == slug:
            if remote.protection is None:
                raise RemoteRefusal(404, "Branch not protected")
            return 200, remote.protection

        match = COMPARE.match(path)
        if match and method == "GET" and match["slug"] == slug:
            behind = remote.behind_by(unquote(match["base"]), unquote(match["head"]))
            return 200, {"behind_by": behind, "ahead_by": 0}

        match = CHECKS.match(path)
        if match and method == "GET" and match["slug"] == slug:
            ref = match["ref"]
            for pull in remote.pull_requests.values():
                if pull.head_sha(remote) == ref or pull.head == ref:
                    return 200, {"total_count": len(pull.checks), "check_runs": list(pull.checks)}
            return 200, {"total_count": 0, "check_runs": []}

        match = MERGE.match(path)
        if match and method == "PUT" and match["slug"] == slug:
            payload = body or {}
            sha = remote.merge(
                int(match["number"]),
                sha=payload.get("sha"),
                method=payload.get("merge_method", "squash"),
            )
            return 200, {"merged": True, "sha": sha}

        match = PULL.match(path)
        if match and method == "GET" and match["slug"] == slug:
            return 200, _pull_json(remote, remote.pull(int(match["number"])))

        match = REF.match(path)
        if match and method == "DELETE" and match["slug"] == slug:
            branch = unquote(match["branch"])
            if branch not in remote.branches():
                raise RemoteRefusal(422, f"Reference does not exist: {branch}")
            from .remote import _git

            _git(remote.path, "update-ref", "-d", f"refs/heads/{branch}")
            return 204, None

        raise RemoteRefusal(404, f"no route for {method} {path}")

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
                # The `gh` shim reaches this server too, and says so. Without
                # the header every call would be recorded as `http` and a test
                # asserting which door was used would assert nothing.
                door = self.headers.get("X-Door") or "http"
                status, payload = double.answer(method, urlparse(self.path).path, body, door)
                encoded = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if encoded:
                    self.wfile.write(encoded)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_PUT(self) -> None:
                self._serve("PUT")

            def do_POST(self) -> None:
                self._serve("POST")

            def do_DELETE(self) -> None:
                self._serve("DELETE")

        return Handler


GH_SHIM = r'''#!/usr/bin/env python3
"""A `gh` that answers from the fixture remote, not from GitHub.

Written by `sd_db.testing.github.install_gh` onto a directory the test puts
first on PATH. It translates the invocations the pack makes into the REST
calls above and prints what `gh` prints, so the caller under test is
unmodified.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ["SD_FIXTURE_GITHUB"]
SLUG = os.environ["SD_FIXTURE_SLUG"]


def call(method, path, body=None):
    """Every request carries `X-Door: gh`, which is how the double knows."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Door": "gh"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, (json.loads(raw) if raw else None)


def fail(status, payload):
    message = (payload or {}).get("message", "gh: request failed")
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


def main(argv):
    if not argv:
        fail(2, {"message": "gh: no arguments"})
    if argv[0] == "auth" and argv[1:2] == ["status"]:
        sys.stderr.write("Logged in to github.com as fixture\n")
        return 0
    if argv[0] == "api":
        method = "GET"
        rest = []
        index = 1
        while index < len(argv):
            token = argv[index]
            if token == "--method":
                method = argv[index + 1]
                index += 2
                continue
            if token in ("--paginate", "--silent"):
                index += 1
                continue
            if token == "--jq":
                index += 2
                continue
            rest.append(token)
            index += 1
        path = rest[0] if rest else ""
        if not path.startswith("/"):
            path = "/" + path
        status, payload = call(method, path)
        if status >= 400:
            fail(status, payload)
        if payload is not None:
            sys.stdout.write(json.dumps(payload) + "\n")
        return 0
    if argv[0] == "pr":
        return pr(argv[1:])
    fail(2, {"message": f"gh: unsupported command {argv[0]}"})


def pr(argv):
    if not argv:
        fail(2, {"message": "gh pr: no subcommand"})
    verb, rest = argv[0], argv[1:]
    if verb == "list":
        status, payload = call("GET", f"/repos/{SLUG}/pulls")
        if status >= 400:
            fail(status, payload)
        sys.stdout.write(json.dumps([shape(pull) for pull in payload]) + "\n")
        return 0
    if verb == "view":
        number = int(rest[0])
        status, payload = call("GET", f"/repos/{SLUG}/pulls/{number}")
        if status >= 400:
            fail(status, payload)
        sys.stdout.write(json.dumps(shape(payload)) + "\n")
        return 0
    if verb == "merge":
        number = int(rest[0])
        sha = None
        method = "merge"
        index = 1
        while index < len(rest):
            token = rest[index]
            if token == "--match-head-commit":
                sha = rest[index + 1]
                index += 2
                continue
            if token in ("--squash", "--rebase", "--merge"):
                method = token.lstrip("-")
                index += 1
                continue
            if token in ("-t", "--title", "-b", "--body"):
                index += 2
                continue
            index += 1
        body = {"merge_method": method}
        if sha is not None:
            body["sha"] = sha
        status, payload = call("PUT", f"/repos/{SLUG}/pulls/{number}/merge", body)
        if status >= 400:
            fail(status, payload)
        sys.stdout.write("Merged\n")
        return 0
    fail(2, {"message": f"gh pr: unsupported subcommand {verb}"})


def shape(pull):
    """The `--json` field names, which are not the REST field names."""
    return {
        "number": pull["number"],
        "title": pull["title"],
        "body": pull["body"],
        "state": pull["state"].upper(),
        "isDraft": pull["draft"],
        "mergeable": "MERGEABLE" if pull["mergeable"] else "CONFLICTING",
        "mergeStateStatus": pull["mergeable_state"].upper(),
        "headRefName": pull["head"]["ref"],
        "headRefOid": pull["head"]["sha"],
        "baseRefName": pull["base"]["ref"],
        "mergeCommit": {"oid": pull["merge_commit_sha"]} if pull["merge_commit_sha"] else None,
        "reviewDecision": "",
        "headRepositoryOwner": {"login": SLUG.split("/")[0]},
        "url": f"https://github.com/{SLUG}/pull/{pull['number']}",
        "statusCheckRollup": [],
    }


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def install_gh(double: GitHubDouble, bin_dir: Path) -> Path:
    """Write a `gh` onto `bin_dir` that answers from `double`.

    The caller puts `bin_dir` first on PATH. Returns the path written, so a
    test can assert on it rather than reconstructing it.
    """
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "gh"
    target.write_text(GH_SHIM, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def gh_environment(double: GitHubDouble, bin_dir: Path, base: os._Environ | dict | None = None) -> dict[str, str]:
    """The environment a caller needs to reach the shim and nothing else."""
    environment = dict(base if base is not None else os.environ)
    environment["PATH"] = f"{Path(bin_dir)}{os.pathsep}{environment.get('PATH', '')}"
    environment["SD_FIXTURE_GITHUB"] = double.base_url
    environment["SD_FIXTURE_SLUG"] = double.remote.slug
    return environment
