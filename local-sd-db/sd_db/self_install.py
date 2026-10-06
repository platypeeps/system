"""A satellite installs the hub's `sd_db` build itself (sd:2802).

`docs/work/2026-10-05-satellite-self-install/`. After the hub's `sd_db`
changes, the hub refuses every session of a satellite that runs the old
build (`remote.BuildMismatch`). Until this module, the operator then ran
`sd-db.sh install <venv>` by hand on the satellite.

`install_hub_build` installs the hub's build into the virtual environment
that holds this package, and only when it can prove the bytes are the
hub's. It fetches `origin` in the source checkout, exports
`origin/main:local-sd-db` with `git archive` into a temporary folder (the
checkout's worktree is never touched), and hashes that `sd_db` with the
digest the handshake uses (`remote.tree_digest`). Only an equal digest is
built and installed. A fresh `python -I` in the venv must then name the
hub's digest, or the install is reported as unverified.

Two callers:

* `after_refusal`, from `hub.Hub.open`: a satellite's command met the
  refusal before the hub ran any statement. It installs, prints one line
  on stderr, and runs the same command once more (`RERUN` stops a loop).
* `sd_db.satellite --apply`, the nightly machine-setup stage.

Guards: `OFF` switches it off; a lock in the venv serialises two
installers; a loopback hub (the hub's own machine) is left alone; a
process that already opened a session does not rerun; a satellite newer
than the hub is not downgraded; and a hub older than this module, whose
refusal carries no digest, gets today's error plus the reason.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import remote, runner_journal

#: Set to `0`, `off`, `false`, `no` or `disabled` to switch self-install off.
OFF = "SD_SATELLITE_SELF_INSTALL"
OFF_WORDS = frozenset({"0", "off", "false", "no", "disabled"})
#: Set in the rerun's environment, so a second refusal installs nothing.
RERUN = "SD_SATELLITE_SELF_INSTALL_RERUN"
#: The system checkout to install from, over the venv's record of it.
SOURCE = "SD_DB_SOURCE_CHECKOUT"
#: In the venv: the checkout the last `sd-db.sh install` or self-install used.
MARKER = "sd-db-source"
#: In the venv: held while one process installs.
LOCK = "sd-db-self-install.lock"
BRANCH = "origin/main"
LIBRARY = "local-sd-db"

#: Seconds for each git step, for each build, probe and pip step, and for
#: the wait on another installer's lock.
GIT_TIMEOUT = 60.0
STEP_TIMEOUT = 300.0
LOCK_WAIT = 600.0
_LOCK_POLL = 0.1

#: This package's folder: the venv that holds it is the one to install into.
PACKAGE = Path(__file__).parent

_PROBE = "from sd_db import remote; print(remote.build_digest())"
_BUILD = ("import sys; sys.path.insert(0, sys.argv[1]); import _build; "
          "print(_build.build_wheel(sys.argv[2]))")


@dataclass(frozen=True)
class Outcome:
    """Whether the hub's build is installed now, and one line that says what happened."""

    installed: bool
    text: str


class _Refused(Exception):
    """A step that stops the install; its text is the reason."""


def enabled(environ) -> bool:
    return environ.get(OFF, "").strip().lower() not in OFF_WORDS


def hub_digest(mismatch: remote.BuildMismatch) -> str | None:
    """The hub's build digest from its refusal, or `None` from an older hub.

    A hub older than sd:2802 names its digest only when the digest is the
    field that differs.
    """
    if getattr(mismatch, "hub_build", None):
        return str(mismatch.hub_build)
    if mismatch.field == "build":
        return mismatch.hub
    return None


def venv_of(package: Path) -> Path | None:
    """The virtual environment whose `site-packages` holds `package`, or `None`."""
    site = Path(package).parent
    if site.name != "site-packages":
        return None
    venv = site.parent.parent.parent
    return venv if (venv / "pyvenv.cfg").is_file() else None


def _pip_record(venv: Path) -> Path | None:
    """The checkout pip installed `sd_db` from, when it was a `git+file` install.

    The command pack provisions `sd_db` that way, so its venv names the
    checkout before any `sd-db.sh install` has written the marker.
    """
    for record in sorted(venv.glob("lib/python*/site-packages/sd_db-*.dist-info/direct_url.json")):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        vcs = data.get("vcs_info") if isinstance(data, dict) else None
        url = urlparse(str(data.get("url", ""))) if isinstance(data, dict) else None
        if isinstance(vcs, dict) and vcs.get("vcs") == "git" and url is not None and url.scheme == "file":
            return Path(unquote(url.path))
    return None


def source_checkout(venv: Path, environ, explicit: Path | None = None) -> tuple[Path | None, str]:
    """The system checkout to install from, and where that answer came from.

    In order: `explicit`, then `SOURCE`, then the venv's `MARKER`, then
    pip's record of a `git+file` install. `None` and the remedy otherwise.
    """
    if explicit is not None:
        return Path(explicit), "named by the caller"
    value = str(environ.get(SOURCE, "")).strip()
    if value:
        return Path(value).expanduser(), f"named by {SOURCE}"
    marker = venv / MARKER
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return Path(text), f"recorded in {marker}"
    recorded = _pip_record(venv)
    if recorded is not None:
        return recorded, "pip's record of the install"
    return None, (f"no source checkout is known for {venv}: set {SOURCE} to the system checkout, "
                  f"or run `sd-db.sh install {venv}` once from it")


def _run(argv: list[str], what: str, timeout: float, *, text: bool = True,
         env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(argv, capture_output=True, text=text, timeout=timeout,  # nosec B603
                              env=env, check=False)
    except subprocess.TimeoutExpired:
        raise _Refused(f"{what} did not finish in {timeout:g} s; nothing installed") from None
    except OSError as error:
        raise _Refused(f"{what} could not start ({error.strerror or error}); nothing installed") from None
    if done.returncode != 0:
        stderr = done.stderr if text else done.stderr.decode("utf-8", "replace")
        last = stderr.strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise _Refused(f"{what} failed: {last[0]}")
    return done


def _git(checkout: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return _run(["git", "-C", str(checkout), *args], f"git {args[0]} in {checkout}", GIT_TIMEOUT,
                text=text, env=env)


def installed_digest(python: Path) -> str | None:
    """The build digest a fresh `python -I` reads from its installed `sd_db`, or `None`."""
    try:
        done = _run([str(python), "-I", "-c", _PROBE], f"{python} reading its sd_db", STEP_TIMEOUT)
    except _Refused:
        return None
    return done.stdout.strip() or None


def pip_install(python: Path, wheel: Path) -> None:
    """Install `wheel` with the venv's pip, offline, over whatever is there."""
    _run([str(python), "-m", "pip", "install", "--quiet", "--no-index", "--force-reinstall", str(wheel)],
         f"pip install of {wheel.name}", STEP_TIMEOUT)


def _locked(path: Path):
    """Hold `path` exclusively, waiting up to `LOCK_WAIT` for another installer.

    Through the one hardened lock opener both packages share.
    """
    return runner_journal.lock(
        path, blocking=False, noun="sd_db self-install", error=_Refused, wait=LOCK_WAIT, poll=_LOCK_POLL,
        held=f"another install held the lock {path} for {LOCK_WAIT:g} s; nothing installed")


def _export(checkout: Path, commit: str, into: Path) -> Path:
    """`commit:local-sd-db` from git into `into`; the worktree is not read."""
    archive = _git(checkout, "archive", "--format=tar", commit, LIBRARY, text=False).stdout
    into.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into / LIBRARY


def _build_wheel(python: Path, library: Path, dist: Path) -> Path:
    dist.mkdir()
    done = _run([str(python), "-I", "-c", _BUILD, str(library), str(dist)],
                f"building the sd_db wheel of {library}", STEP_TIMEOUT)
    return dist / done.stdout.strip()


def _install_locked(hub_build: str, venv: Path, checkout: Path) -> Outcome:
    python = venv / "bin" / "python"
    # Another process may have installed it while this one waited.
    if installed_digest(python) == hub_build:
        return Outcome(True, f"the hub's sd_db build {hub_build} is already installed in {venv}")
    _git(checkout, "fetch", "--quiet", "origin")
    commit = _git(checkout, "rev-parse", "--verify", "--quiet", f"{BRANCH}^{{commit}}").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="sd-db-self-install.") as folder:
        library = _export(checkout, commit, Path(folder) / "export")
        found = remote.tree_digest(library / "sd_db")
        if found != hub_build:
            raise _Refused(f"{BRANCH} ({commit[:12]}) of {checkout} builds {found} and the hub runs "
                           f"{hub_build}: the hub runs a build that is not {BRANCH}'s tip; nothing installed")
        wheel = _build_wheel(python, library, Path(folder) / "dist")
        pip_install(python, wheel)
    after = installed_digest(python)
    if after != hub_build:
        raise _Refused(f"installed {BRANCH} ({commit[:12]}) into {venv}, and a fresh interpreter there "
                       f"reads build {after}, not the hub's {hub_build}: the install did not verify")
    text = (f"installed the hub's sd_db build {hub_build} ({BRANCH} {commit[:12]} of {checkout}) "
            f"into {venv}")
    try:
        (venv / MARKER).write_text(f"{checkout.resolve()}\n", encoding="utf-8")
    except OSError as error:
        text += f"; could not record the source in {venv / MARKER} ({error.strerror or error})"
    return Outcome(True, text)


def install_hub_build(hub_build: str, *, venv: Path | None = None, source: Path | None = None,
                      environ=None) -> Outcome:
    """Install the hub's build `hub_build` from `origin/main`, only if it is that build.

    `venv` defaults to the one holding this package; `source` to the
    checkout `source_checkout` finds. Never raises for a refused step: the
    outcome names it.
    """
    environ = os.environ if environ is None else environ
    if not enabled(environ):
        return Outcome(False, f"self-install is off ({OFF}={environ.get(OFF)})")
    if venv is None:
        venv = venv_of(PACKAGE)
        if venv is None:
            return Outcome(False, f"this sd_db at {PACKAGE} is not installed in a virtual environment")
    checkout, why = source_checkout(venv, environ, source)
    if checkout is None:
        return Outcome(False, why)
    try:
        with _locked(venv / LOCK):
            return _install_locked(hub_build, venv, checkout)
    except _Refused as refused:
        return Outcome(False, str(refused))


def _why_not(mismatch: remote.BuildMismatch, environ) -> str | None:
    """Why this refusal is not one to install for, or `None` when it is."""
    if not enabled(environ):
        return f"self-install is off ({OFF}={environ.get(OFF)})"
    if environ.get(RERUN):
        return ("this is the rerun after a self-install, and the hub still refuses it; "
                "install the hub's build by hand")
    if remote.sessions_opened():
        return ("this process reached the hub before, so it does not install and rerun; "
                "run the command again")
    if hub_digest(mismatch) is None:
        return "the hub sent no build digest (a hub older than sd:2802); install its build by hand"
    if mismatch.upgrade == "hub":
        return "this satellite's build is newer than the hub's; upgrade the hub, not this satellite"
    return None


def _with_reason(mismatch: remote.BuildMismatch, reason: str) -> remote.BuildMismatch:
    """Today's refusal, same class and fields, with why nothing was installed."""
    refused = type(mismatch).__new__(type(mismatch))
    refused.__dict__.update(vars(mismatch))
    refused.args = (f"{mismatch}. This satellite did not install the hub's build: {reason}",)
    return refused


def rerun(environ, execve=os.execve) -> None:
    """Replace this process with the same command, marked as the rerun."""
    for stream in (sys.stdout, sys.stderr):
        stream.flush()
    execve(sys.executable, sys.orig_argv, {**environ, RERUN: "1"})


def after_refusal(mismatch: remote.BuildMismatch, *, loopback: bool, environ=None,
                  execve=os.execve, err=None, install=None) -> BaseException:
    """Install the hub's build and rerun this command, or the error to raise.

    Called where the satellite's open met the refusal, so the hub ran no
    statement of this command. On an install it does not return: the
    process is replaced. `install` and `execve` are seams for tests.
    """
    if loopback:
        return mismatch
    environ = os.environ if environ is None else environ
    err = sys.stderr if err is None else err
    install = install_hub_build if install is None else install
    reason = _why_not(mismatch, environ)
    if reason is not None:
        return _with_reason(mismatch, reason)
    outcome = install(hub_digest(mismatch), environ=environ)
    if not outcome.installed:
        return _with_reason(mismatch, outcome.text)
    err.write(f"sd_db: {outcome.text}; running the command again\n")
    err.flush()
    try:
        rerun(environ, execve)
    except OSError as error:
        return _with_reason(mismatch, f"{outcome.text}, and the rerun failed ({error}); "
                                      f"run the command again")
    return mismatch
