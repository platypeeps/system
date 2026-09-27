"""Stubs for the six commands the system shells out to and cannot run here.

`launchctl`, `tailscale`, `curl`, `caffeinate`, `lsof` and `local-notify` all
touch something a test must not touch -- the user's real launchd domain, the
real tailnet, the network, the display's sleep state, the machine's open
ports, and Notification Center. Each is replaced by a script on a directory
the test puts first on PATH.

They are stubs and not mocks. Nothing patches `subprocess`; the caller under
test spawns the same command line it spawns in production and the stub is
what answers. That is the difference criterion 23 asks for: a suite that
patches `subprocess` around `launchctl` proves the call was made, while a
suite that puts a `launchctl` on PATH proves the call was made *and that it
was spelled correctly* -- an argument the real command would reject fails
here too, because the stub parses the same arguments.

Three of them are not inert:

* `curl` performs the request, through `urllib`, and records it. Tests point
  the registry at `ProviderDouble` and the pack at `GitHubDouble`, so a `curl`
  that refused to make the call would hide every caller that reaches an
  endpoint by shelling out instead of through the library.
* `caffeinate` runs the command it wraps. `caffeinate -i make check` that
  swallowed `make check` would turn a failing build green.
* `launchctl` keeps a table, so `bootstrap` then `list` shows the job.

The other three answer from state the test sets and nothing else.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

#: Every command this module can install. A test that wants the lot passes
#: no names; enumerating them here keeps the list in one place.
STUB_NAMES = ("launchctl", "tailscale", "curl", "caffeinate", "lsof", "local-notify")

PREAMBLE = r'''#!/usr/bin/env python3
"""A stub written by `sd_db.testing.stubs`. It records, then answers."""
import json
import os
import stat
import sys

NAME = os.path.basename(sys.argv[0])
STATE = os.path.join(os.environ["SD_FIXTURE_STUBS"], NAME + ".json")
CALLS = os.path.join(os.environ["SD_FIXTURE_STUBS"], NAME + ".jsonl")


def state():
    try:
        with open(STATE, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def save(value):
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(value, handle)


def read_stdin():
    """Read piped input without ever blocking on a pipe nobody writes to.

    See the note in `sd_db.testing.providers`: an inherited pipe is not a
    terminal, and reading it waits for a writer that may never close.
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


def record(argv, status, stdin=None):
    with open(CALLS, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "argv": argv, "status": status, "cwd": os.getcwd(), "stdin": stdin,
        }) + "\n")
    return status
'''

LAUNCHCTL = PREAMBLE + r'''

def main(argv):
    """`list`, `print`, `bootstrap`, `bootout`, `kickstart`, `enable` -- what the
    system uses. The loaded set lives in the state file, so `bootstrap` then
    `list` shows the job the way the real one does."""
    jobs = state().get("jobs", {})
    if not argv:
        return record(argv, 1)
    verb = argv[0]
    if verb == "list":
        if len(argv) > 1:
            label = argv[1]
            if label not in jobs:
                sys.stderr.write(f"Could not find service {label}\n")
                return record(argv, 113)
            job = jobs[label]
            sys.stdout.write(
                "{\n\t\"PID\" = %s;\n\t\"LastExitStatus\" = %s;\n\t\"Label\" = \"%s\";\n};\n"
                % (job.get("pid", 0), job.get("status", 0), label)
            )
            return record(argv, 0)
        sys.stdout.write("PID\tStatus\tLabel\n")
        for label, job in sorted(jobs.items()):
            sys.stdout.write(f"{job.get('pid', '-')}\t{job.get('status', 0)}\t{label}\n")
        return record(argv, 0)
    if verb == "print":
        target = argv[1] if len(argv) > 1 else ""
        label = target.rsplit("/", 1)[-1]
        if label not in jobs:
            sys.stderr.write(f"Could not find service \"{label}\"\n")
            return record(argv, 113)
        job = jobs[label]
        sys.stdout.write(
            f"{target} = {{\n\tstate = {job.get('state', 'running')}\n"
            f"\tpid = {job.get('pid', 0)}\n\tlast exit code = {job.get('status', 0)}\n}}\n"
        )
        return record(argv, 0)
    if verb == "enable":
        if len(argv) != 2 or len(argv[1].split("/")) != 3:
            return record(argv, 64)
        held = state()
        held.setdefault("enabled", {})[argv[1]] = True
        save(held)
        return record(argv, 0)
    if verb == "bootstrap":
        plist = argv[-1]
        label = os.path.basename(plist)
        if label.endswith(".plist"):
            label = label[: -len(".plist")]
        if label in jobs:
            sys.stderr.write("Load failed: 5: Input/output error\n")
            return record(argv, 5)
        jobs[label] = {"pid": 4321, "status": 0, "state": "running", "plist": plist}
        save({"jobs": jobs})
        return record(argv, 0)
    if verb in ("bootout", "kickstart"):
        target = argv[-1]
        label = target.rsplit("/", 1)[-1]
        if label.startswith("-"):
            label = argv[-1]
        if label not in jobs:
            sys.stderr.write(f"Could not find service \"{label}\"\n")
            return record(argv, 113)
        if verb == "bootout":
            del jobs[label]
        else:
            jobs[label]["pid"] = jobs[label].get("pid", 0) + 1
        save({"jobs": jobs})
        return record(argv, 0)
    sys.stderr.write(f"Unrecognized subcommand: {verb}\n")
    return record(argv, 64)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

TAILSCALE = PREAMBLE + r'''

def main(argv):
    """`status`, `status --json`, `ip -4`. The answer is the state file."""
    current = state()
    if not argv:
        return record(argv, 1)
    if argv[0] == "status":
        if current.get("backend", "Running") != "Running":
            sys.stderr.write("Tailscale is stopped.\n")
            return record(argv, 1)
        if "--json" in argv:
            sys.stdout.write(json.dumps({
                "BackendState": current.get("backend", "Running"),
                "Self": {
                    "HostName": current.get("hostname", "fixture"),
                    "TailscaleIPs": current.get("ips", ["100.64.0.1"]),
                    "Online": True,
                },
                "Peer": current.get("peers", {}),
            }) + "\n")
        else:
            for ip in current.get("ips", ["100.64.0.1"]):
                sys.stdout.write(f"{ip}\t{current.get('hostname', 'fixture')}\n")
        return record(argv, 0)
    if argv[0] == "ip":
        ips = current.get("ips", ["100.64.0.1"])
        sys.stdout.write(ips[0] + "\n")
        return record(argv, 0)
    sys.stderr.write(f"tailscale: unknown subcommand {argv[0]}\n")
    return record(argv, 1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

CURL = PREAMBLE + r'''
import urllib.error
import urllib.request


def main(argv):
    """Perform the request and record it.

    The flags parsed are the ones the system passes: `-s`, `-S`, `-f`,
    `-X/--request`, `-H/--header`, `-d/--data`, `-o/--output`, `-w`,
    `--max-time`, `--retry`. A refusal to make the call would hide any
    caller reaching an endpoint by shell instead of through the library, so
    the request is real -- it just goes to whatever the test started.
    """
    method = None
    headers = {}
    data = None
    output = None
    write_out = None
    fail = False
    url = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("-X", "--request"):
            method, index = argv[index + 1], index + 2
        elif token in ("-H", "--header"):
            name, _, value = argv[index + 1].partition(":")
            headers[name.strip()] = value.strip()
            index += 2
        elif token in ("-d", "--data", "--data-raw", "--data-binary"):
            data, index = argv[index + 1], index + 2
        elif token in ("-o", "--output"):
            output, index = argv[index + 1], index + 2
        elif token == "-w":
            write_out, index = argv[index + 1], index + 2
        elif token in ("--max-time", "--retry", "--connect-timeout", "--retry-delay"):
            index += 2
        elif token in ("-f", "--fail"):
            fail, index = True, index + 1
        elif token in ("-s", "--silent", "-S", "--show-error", "-L", "--location"):
            index += 1
        elif token.startswith("-") and token != "-":
            index += 1
        else:
            url, index = token, index + 1

    if url is None:
        sys.stderr.write("curl: no URL specified\n")
        return record(argv, 2)

    payload = data.encode() if data is not None else None
    request = urllib.request.Request(
        url, data=payload, method=method or ("POST" if payload else "GET"), headers=headers
    )
    try:
        with urllib.request.urlopen(request) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
        if fail:
            sys.stderr.write(f"curl: (22) The requested URL returned error: {status}\n")
            emit(write_out, status)
            return record(argv, 22, data)
    except OSError as error:
        sys.stderr.write(f"curl: (7) Failed to connect: {error}\n")
        return record(argv, 7, data)

    if output and output != "-":
        with open(output, "wb") as handle:
            handle.write(body)
    else:
        sys.stdout.buffer.write(body)
    emit(write_out, status)
    return record(argv, 0, data)


def emit(write_out, status):
    """`-w '%{http_code}'` is how the system reads the status. Nothing else
    in the format is substituted, because nothing else is asked for."""
    if write_out:
        sys.stdout.write(write_out.replace("%{http_code}", str(status)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

CAFFEINATE = PREAMBLE + r'''
import subprocess


def main(argv):
    """Drop the assertion flags and run whatever is left.

    A stub that swallowed the wrapped command would turn a failing build
    green, so the command runs and its status is this process's status.
    """
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        if argv[index] in ("-t", "-w"):
            index += 2
            continue
        index += 1
    rest = argv[index:]
    if not rest:
        return record(argv, 0)
    completed = subprocess.run(rest)
    return record(argv, completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

LSOF = PREAMBLE + r'''

def main(argv):
    """`-i :PORT` and `-t`. Ports come from the state file.

    The real `lsof` exits 1 when nothing matches, and the callers branch on
    that status rather than on empty output, so the status matters more than
    the table.
    """
    ports = {str(key): value for key, value in state().get("ports", {}).items()}
    wanted = None
    terse = False
    for token in argv:
        if token.startswith("-i"):
            rest = token[2:]
            wanted = rest.lstrip(":") or None
        elif token.startswith(":") :
            wanted = token.lstrip(":")
        elif token == "-t":
            terse = True
    rows = []
    for port, owner in sorted(ports.items()):
        if wanted in (None, port):
            rows.append((port, owner))
    if not rows:
        return record(argv, 1)
    if terse:
        for _port, owner in rows:
            sys.stdout.write(f"{owner.get('pid', 0)}\n")
    else:
        sys.stdout.write("COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n")
        for port, owner in rows:
            sys.stdout.write(
                f"{owner.get('command', 'fixture')} {owner.get('pid', 0)} "
                f"{os.environ.get('USER', 'fixture')} 7u IPv4 0x0 0t0 TCP *:{port} (LISTEN)\n"
            )
    return record(argv, 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

LOCAL_NOTIFY = PREAMBLE + r'''

def main(argv):
    """Record the notification. Nothing reaches Notification Center."""
    return record(argv, 0, read_stdin())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

SCRIPTS = {
    "launchctl": LAUNCHCTL,
    "tailscale": TAILSCALE,
    "curl": CURL,
    "caffeinate": CAFFEINATE,
    "lsof": LSOF,
    "local-notify": LOCAL_NOTIFY,
}


@dataclass
class StubCall:
    """One invocation of a stub, in the order it happened."""

    name: str
    argv: list[str]
    status: int
    cwd: str
    stdin: str | None = None


class Stubs:
    """The installed stubs, the state they answer from, and what they were asked.

    `bin_dir` goes first on PATH; `environment()` builds that. `state(name)`
    sets what a stub answers with -- loaded jobs, tailnet addresses, open
    ports. `calls(name)` is what it was asked, in order.
    """

    def __init__(self, root: Path, names: tuple[str, ...] = STUB_NAMES) -> None:
        self.root = Path(root)
        self.bin = self.root / "bin"
        self.data = self.root / "state"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.names = names
        for name in names:
            self._install(name)

    def _install(self, name: str) -> Path:
        try:
            source = SCRIPTS[name]
        except KeyError:
            raise ValueError(f"no stub for {name!r}; known: {', '.join(sorted(SCRIPTS))}") from None
        target = self.bin / name
        target.write_text(source, encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return target

    # -------------------------------------------------------------- state

    def state(self, name: str, value: dict) -> None:
        """Set what `name` answers from. Replaces; it does not merge."""
        if name not in self.names:
            raise ValueError(f"{name!r} is not installed here")
        (self.data / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")

    def read_state(self, name: str) -> dict:
        path = self.data / f"{name}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    # -------------------------------------------------------------- calls

    def calls(self, name: str) -> list[StubCall]:
        path = self.data / f"{name}.jsonl"
        if not path.exists():
            return []
        return [
            StubCall(
                name=name,
                argv=entry["argv"],
                status=entry["status"],
                cwd=entry["cwd"],
                stdin=entry.get("stdin"),
            )
            for entry in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        ]

    def all_calls(self) -> list[StubCall]:
        """Every stub's calls. Not globally ordered: each file is its own."""
        return [call for name in self.names for call in self.calls(name)]

    # -------------------------------------------------------- environment

    def environment(self, base: os._Environ | dict | None = None) -> dict[str, str]:
        environment = dict(base if base is not None else os.environ)
        environment["PATH"] = f"{self.bin}{os.pathsep}{environment.get('PATH', '')}"
        environment["SD_FIXTURE_STUBS"] = str(self.data)
        return environment
