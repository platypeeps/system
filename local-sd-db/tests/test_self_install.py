"""sd:2802: a satellite installs the hub's sd_db build itself.

`docs/work/2026-10-05-satellite-self-install/`. A satellite whose build the
hub refuses installs the hub's build from `origin/main` of its source
checkout, but only when that tip's digest equals the hub's. It installs on a
refusal, then runs the same command once more, and in the nightly
`sd_db.satellite --apply`.

Every origin here is a temporary git repository, every virtual environment
is a fresh `venv --without-pip`, and the pip step is a stand-in that unpacks
the wheel. Nothing reaches a network or a real hub.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sd_db import hub, remote, satellite, self_install

from tests.test_wire import Served, newer_build

HERE = Path(__file__).resolve().parents[1]

#: Git with no operator config: no signing, no hooks, a fixed author.
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True,
                          check=True, timeout=60)
    return done.stdout.strip()


class Origin:
    """A work repository, its bare `origin`, and a source checkout cloned from it.

    The library in it is a copy of this `local-sd-db`: its package, its build
    backend and its metadata.
    """

    def __init__(self, root: Path) -> None:
        self.work = root / "work"
        library = self.work / self_install.LIBRARY
        library.mkdir(parents=True)
        shutil.copytree(HERE / "sd_db", library / "sd_db", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("_build.py", "pyproject.toml"):
            shutil.copy2(HERE / name, library / name)
        git(self.work, "init", "--quiet", "-b", "main")
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", "the library")
        self.bare = root / "origin.git"
        git(root, "clone", "--quiet", "--bare", str(self.work), str(self.bare))
        self.source = root / "source"
        git(root, "clone", "--quiet", str(self.bare), str(self.source))

    def digest(self) -> str:
        """The build digest of the work repository's tip."""
        return remote.tree_digest(self.work / self_install.LIBRARY / "sd_db")

    def push_change(self, text: str) -> str:
        """A new tip on `origin/main` whose `reads.py` carries `text`; its digest."""
        changed = self.work / self_install.LIBRARY / "sd_db" / "reads.py"
        with open(changed, "a") as handle:
            handle.write(f"\n# {text}\n")
        git(self.work, "commit", "--quiet", "-am", text)
        git(self.work, "push", "--quiet", str(self.bare), "main")
        return self.digest()


def make_venv(root: Path) -> Path:
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   capture_output=True, timeout=120)
    return venv


def site_packages(venv: Path) -> Path:
    (found,) = venv.glob("lib/python*/site-packages")
    return found


class Unpacker:
    """The pip step's stand-in: unpack the wheel into the venv, as pip would."""

    def __init__(self, venv: Path, *, after=None) -> None:
        self.venv = venv
        self.calls: list[tuple[Path, Path]] = []
        self.after = after

    def __call__(self, python: Path, wheel: Path) -> None:
        self.calls.append((Path(python), Path(wheel)))
        site = site_packages(self.venv)
        shutil.rmtree(site / "sd_db", ignore_errors=True)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)
        if self.after is not None:
            self.after(site)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = Origin(self.root)
        self.venv = make_venv(self.root)
        self.unpack = Unpacker(self.venv)

    def install(self, digest: str, *, environ: dict | None = None, source: Path | None = None):
        with mock.patch.object(self_install, "pip_install", self.unpack):
            return self_install.install_hub_build(
                digest, venv=self.venv, source=self.origin.source if source is None else source,
                environ={} if environ is None else environ)

    def installed(self) -> str | None:
        return self_install.installed_digest(self.venv / "bin" / "python")


class TheInstall(Case):
    """The library function: install only bytes whose digest equals the hub's."""

    def test_an_equal_digest_installs_the_fetched_origin_main_and_verifies_it(self):
        head = git(self.origin.source, "rev-parse", "HEAD")
        # The hub runs a tip this checkout has not fetched yet.
        wanted = self.origin.push_change("the hub's build")
        outcome = self.install(wanted)
        self.assertTrue(outcome.installed, outcome.text)
        self.assertEqual(self.installed(), wanted)
        self.assertEqual(len(self.unpack.calls), 1)
        self.assertIn(wanted, outcome.text)
        # The checkout's worktree and HEAD are untouched; only its refs moved.
        self.assertEqual(git(self.origin.source, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.origin.source, "status", "--porcelain"), "")
        self.assertNotIn("the hub's build", (self.origin.source / self_install.LIBRARY / "sd_db"
                                             / "reads.py").read_text())
        self.assertEqual((self.venv / self_install.MARKER).read_text().strip(),
                         str(self.origin.source.resolve()))

    def test_a_tip_that_is_not_the_hubs_build_installs_nothing(self):
        hubs = self.origin.digest()
        self.origin.push_change("a merge the hub has not restarted on")
        outcome = self.install(hubs)
        self.assertFalse(outcome.installed)
        self.assertIn("origin/main", outcome.text)
        self.assertIn(hubs, outcome.text)
        self.assertEqual(self.unpack.calls, [])
        self.assertIsNone(self.installed())

    def test_the_off_switch_installs_and_fetches_nothing(self):
        before = git(self.origin.source, "rev-parse", "origin/main")
        wanted = self.origin.push_change("the hub's build")
        for word in ("0", "off", "false", "no", "disabled"):
            outcome = self.install(wanted, environ={self_install.OFF: word})
            self.assertFalse(outcome.installed)
            self.assertIn(f"{self_install.OFF}={word}", outcome.text)
        self.assertEqual(self.unpack.calls, [])
        self.assertEqual(git(self.origin.source, "rev-parse", "origin/main"), before)

    def test_a_held_lock_installs_nothing_at_its_bound(self):
        wanted = self.origin.digest()
        with open(self.venv / self_install.LOCK, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch.object(self_install, "LOCK_WAIT", 0.3):
                outcome = self.install(wanted)
        self.assertFalse(outcome.installed)
        self.assertIn("lock", outcome.text)
        self.assertEqual(self.unpack.calls, [])

    def test_an_install_another_process_finished_is_not_repeated(self):
        wanted = self.origin.digest()
        self.assertTrue(self.install(wanted).installed)
        again = self.install(wanted)
        self.assertTrue(again.installed, again.text)
        self.assertIn("already", again.text)
        self.assertEqual(len(self.unpack.calls), 1)

    def test_an_install_that_does_not_verify_is_reported(self):
        def tamper(site: Path) -> None:
            with open(site / "sd_db" / "reads.py", "a") as handle:
                handle.write("\n# not the hub's\n")

        self.unpack.after = tamper
        outcome = self.install(self.origin.digest())
        self.assertFalse(outcome.installed)
        self.assertIn("verify", outcome.text)

    def test_an_unknown_source_refuses_by_name(self):
        with mock.patch.object(self_install, "pip_install", self.unpack):
            outcome = self_install.install_hub_build(self.origin.digest(), venv=self.venv, environ={})
        self.assertFalse(outcome.installed)
        self.assertIn(self_install.SOURCE, outcome.text)
        self.assertEqual(self.unpack.calls, [])

    def test_the_source_is_explicit_then_the_variable_then_the_marker_then_pips_record(self):
        explicit, variable, marked, recorded = (self.root / name for name in ("e", "v", "m", "r"))
        for folder in (explicit, variable, marked, recorded):
            folder.mkdir()
        (self.venv / self_install.MARKER).write_text(f"{marked}\n")
        info = site_packages(self.venv) / "sd_db-0.1.0.dist-info"
        info.mkdir()
        (info / "direct_url.json").write_text(json.dumps({
            "url": recorded.as_uri(), "subdirectory": "local-sd-db",
            "vcs_info": {"vcs": "git", "commit_id": "0" * 40}}))
        env = {self_install.SOURCE: str(variable)}
        self.assertEqual(self_install.source_checkout(self.venv, env, explicit)[0], explicit)
        self.assertEqual(self_install.source_checkout(self.venv, env)[0], variable)
        self.assertEqual(self_install.source_checkout(self.venv, {})[0], marked)
        (self.venv / self_install.MARKER).unlink()
        self.assertEqual(self_install.source_checkout(self.venv, {})[0], recorded)
        info.joinpath("direct_url.json").unlink()
        found, why = self_install.source_checkout(self.venv, {})
        self.assertIsNone(found)
        self.assertIn(self_install.SOURCE, why)

    def test_the_venv_is_the_one_holding_the_imported_package(self):
        package = site_packages(self.venv) / "sd_db"
        package.mkdir()
        self.assertEqual(self_install.venv_of(package), self.venv)
        self.assertIsNone(self_install.venv_of(HERE / "sd_db"))


def mismatch(field: str = "build", satellite_value="a" * 16, hub_value="b" * 16, *,
             hub_build: str | None = "b" * 16) -> remote.BuildMismatch:
    """A refusal as the satellite rebuilds it; `hub_build=None` is an older hub's."""
    described = remote.describe_error(remote.BuildMismatch(field, satellite_value, hub_value,
                                                           hub_build=hub_build))
    if hub_build is None:
        described["attributes"].pop("hub_build", None)
    return remote.rebuild_error(described)


class TheRefusal(unittest.TestCase):
    """A satellite's command met `BuildMismatch`: install, then run it once more."""

    def setUp(self):
        self.err = io.StringIO()
        self.execs: list[tuple] = []
        self.asked: list[str] = []
        patcher = mock.patch.object(remote, "_sessions", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_exec(self, path, argv, env):
        self.execs.append((path, list(argv), dict(env)))

    def refuse(self, error, *, environ=None, loopback=False, installed=True, unsafe=None):
        def install(digest, **_):
            self.asked.append(digest)
            return self_install.Outcome(installed, "installed the hub's build" if installed
                                        else "origin/main builds another digest")

        return self_install.after_refusal(error, loopback=loopback,
                                          environ={} if environ is None else environ,
                                          execve=self.fake_exec, err=self.err, install=install,
                                          replay=lambda: unsafe)

    def test_a_command_its_argv_cannot_replay_is_installed_for_but_not_rerun(self):
        error = mismatch()
        result = self.refuse(error, unsafe="this program was read from standard input")
        self.assertEqual((self.asked, self.execs), (["b" * 16], []))
        self.assertTrue(str(result).startswith(str(error)))
        self.assertIn("installed the hub's build", str(result))
        self.assertIn("read from standard input. Run it again", str(result))

    def test_an_install_runs_the_same_command_again_once(self):
        self.refuse(mismatch())
        self.assertEqual(self.asked, ["b" * 16])
        (path, argv, env), = self.execs
        self.assertEqual((path, argv), (sys.executable, list(sys.orig_argv)))
        self.assertEqual(env[self_install.RERUN], "1")
        self.assertEqual(len(self.err.getvalue().splitlines()), 1)
        self.assertIn("installed the hub's build", self.err.getvalue())

    def test_the_rerun_that_meets_the_mismatch_again_installs_nothing(self):
        result = self.refuse(mismatch(), environ={self_install.RERUN: "1"})
        self.assertEqual((self.asked, self.execs), ([], []))
        self.assertIsInstance(result, remote.BuildMismatch)
        self.assertIn("rerun", str(result))

    def test_an_older_hub_without_a_digest_keeps_todays_error(self):
        error = mismatch("package", "0.1.0", "0.1.1", hub_build=None)
        result = self.refuse(error)
        self.assertEqual((self.asked, self.execs), ([], []))
        self.assertTrue(str(result).startswith(str(error)), str(result))
        self.assertIn("digest", str(result)[len(str(error)):])

    def test_an_older_hubs_build_refusal_carries_the_digest_as_its_value(self):
        self.refuse(mismatch("build", "a" * 16, "c" * 16, hub_build=None))
        self.assertEqual(self.asked, ["c" * 16])

    def test_a_satellite_newer_than_the_hub_is_not_downgraded(self):
        result = self.refuse(mismatch("package", "0.1.1", "0.1.0"))
        self.assertEqual((self.asked, self.execs), ([], []))
        self.assertIn("upgrade the hub", str(result))

    def test_a_process_that_already_reached_the_hub_does_not_run_again(self):
        with mock.patch.object(remote, "_sessions", 1):
            result = self.refuse(mismatch())
        self.assertEqual((self.asked, self.execs), ([], []))
        self.assertIn("rerun", str(result))

    def test_a_loopback_hub_is_left_alone(self):
        error = mismatch()
        result = self.refuse(error, loopback=True)
        self.assertEqual((self.asked, self.execs), ([], []))
        self.assertIs(result, error)

    def test_the_off_switch_keeps_todays_error_and_names_itself(self):
        result = self.refuse(mismatch(), environ={self_install.OFF: "0"})
        self.assertEqual((self.asked, self.execs), ([], []))
        self.assertIn(self_install.OFF, str(result))

    def test_a_refused_install_keeps_todays_error_and_adds_the_reason(self):
        error = mismatch()
        result = self.refuse(error, installed=False)
        self.assertEqual(self.execs, [])
        self.assertIsInstance(result, remote.BuildMismatch)
        self.assertEqual((result.field, result.hub), (error.field, error.hub))
        self.assertTrue(str(result).startswith(str(error)))
        self.assertIn("origin/main builds another digest", str(result))


class TheReplayCheck(unittest.TestCase):
    """Only a command whose argv runs it again is rerun."""

    def setUp(self):
        self.null = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, self.null)

    def test_a_program_read_from_standard_input_is_not_rerun(self):
        for argv0 in ("-", ""):
            self.assertIn("standard input", self_install.replay_refusal(argv0, self.null))

    def test_a_program_file_that_is_gone_is_not_rerun(self):
        self.assertIn("not there", self_install.replay_refusal("/nonexistent/sd", self.null))

    def test_a_pipe_on_standard_input_is_not_rerun(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        self.assertIn("pipe", self_install.replay_refusal(__file__, read))

    def test_a_script_or_dash_c_with_null_or_closed_input_is_rerun(self):
        self.assertIsNone(self_install.replay_refusal(__file__, self.null))
        self.assertIsNone(self_install.replay_refusal("-c", self.null))
        closed = os.dup(self.null)
        os.close(closed)
        self.assertIsNone(self_install.replay_refusal(__file__, closed))

    def test_an_undeclared_command_is_never_rerun(self):
        with mock.patch.object(self_install, "_replayable", False):
            self.assertIn("did not declare", self_install.rerun_refusal())
        with mock.patch.object(self_install, "_replayable", True), \
                mock.patch.object(self_install, "replay_refusal", return_value=None):
            self.assertIsNone(self_install.rerun_refusal())

    def test_local_work_before_the_refusal_runs_once_without_a_declaration(self):
        """The second review's reproduction (sd:2802): a script's local effect ran twice."""
        script = Path(tempfile.mkdtemp()) / "client.py"
        self.addCleanup(shutil.rmtree, script.parent)
        script.write_text(
            "import os, sys\n"
            "from sd_db import remote, self_install\n"
            "print('LOCAL_EFFECT', flush=True)\n"
            "error = remote.BuildMismatch('build', 'a' * 16, 'b' * 16, hub_build='b' * 16)\n"
            "install = lambda digest, **_: self_install.Outcome(True, 'installed the hub build')\n"
            "raise self_install.after_refusal(error, loopback=False, environ=dict(os.environ),"
            " install=install)\n"
        )
        with open(os.devnull) as null:
            done = subprocess.run([sys.executable, str(script)], stdin=null, capture_output=True, text=True,
                                  check=False, timeout=60, env={**os.environ, "PYTHONPATH": str(HERE)})
        self.assertEqual(done.stdout.count("LOCAL_EFFECT"), 1, done.stdout)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("did not declare", done.stderr)

    def test_python_dash_installs_and_says_run_it_again_instead_of_exiting_quietly(self):
        """The review's reproduction (sd:2802): `python -` reran against an empty stdin."""
        program = (
            "import os, sys\n"
            "from sd_db import remote, self_install\n"
            "error = remote.BuildMismatch('build', 'a' * 16, 'b' * 16, hub_build='b' * 16)\n"
            "def execve(*_):\n"
            "    print('reran'); os._exit(0)\n"
            "install = lambda digest, **_: self_install.Outcome(True, 'installed the hub build')\n"
            "raise self_install.after_refusal(error, loopback=False, environ={}, execve=execve,"
            " install=install)\n"
        )
        done = subprocess.run([sys.executable, "-"], input=program, capture_output=True, text=True,
                              check=False, timeout=60, env={**os.environ, "PYTHONPATH": str(HERE)})
        self.assertNotIn("reran", done.stdout)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("did not run again", done.stderr)


class TheHubOpen(unittest.TestCase):
    """`Hub.open` is the client entry: every satellite open of the default database."""

    def open(self, **fields):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        chosen = hub.Hub(host="hub.example.test", port=8769, config=root / "hub.json", **fields)
        refused = mismatch()
        answer = remote.BuildMismatch("build", "x", "y")
        with mock.patch.object(remote, "connect", side_effect=refused), \
                mock.patch.object(self_install, "after_refusal", return_value=answer) as after:
            with self.assertRaises(remote.BuildMismatch) as raised:
                chosen.open(root / "sd.db", write=False, create=False, busy_timeout=5000)
        self.assertIs(raised.exception, answer)
        (args, kwargs), = after.call_args_list
        self.assertIs(args[0], refused)
        return kwargs["loopback"]

    def test_a_tailnet_hub_may_install(self):
        self.assertFalse(self.open())

    def test_a_loopback_hub_never_installs(self):
        self.assertTrue(self.open(token_file=Path("/nonexistent/token")))


class TheHubsDigest(unittest.TestCase):
    """The refusal carries the hub's build digest, whatever field differs."""

    def test_every_handshake_refusal_names_the_hubs_digest(self):
        ours = remote.handshake()
        for frame in ({"v": remote.PROTOCOL_VERSION + 1, **ours},
                      {"v": remote.PROTOCOL_VERSION, **ours, "package": "0.0.1"},
                      {"v": remote.PROTOCOL_VERSION, **ours, "schema": 1},
                      {"v": remote.PROTOCOL_VERSION, **ours, "build": "0" * 16}):
            with self.assertRaises(remote.BuildMismatch) as raised:
                remote.check_handshake(frame)
            rebuilt = remote.rebuild_error(remote.describe_error(raised.exception))
            self.assertEqual(rebuilt.hub_build, remote.build_digest(), frame)

    def test_the_digest_of_a_tree_is_the_build_digest_of_the_package_in_it(self):
        self.assertEqual(remote.tree_digest(HERE / "sd_db"), remote._hash_files())

    def test_a_newer_hub_names_its_digest_over_the_wire(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        build, _ = newer_build(root)
        served = Served(root, "newer.db", build=build)
        self.addCleanup(served.stop)
        with self.assertRaises(remote.BuildMismatch) as raised:
            served.connect()
        self.assertEqual(raised.exception.field, "package")
        self.assertEqual(raised.exception.hub_build, remote.tree_digest(build / "sd_db"))

    def test_the_servers_protocol_refusal_names_its_digest(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        served = Served(root)
        self.addCleanup(served.stop)
        with socket.create_connection(("127.0.0.1", served.port)) as raw:
            remote.send_frame(raw, {**remote.request("open", path=None, write=False, create=False,
                                                     busy_timeout=5000, token=served.token,
                                                     **remote.handshake()), "v": remote.PROTOCOL_VERSION + 1})
            refused = remote.read_frame(raw)
        rebuilt = remote.rebuild_error(refused["error"])
        self.assertEqual((type(rebuilt), rebuilt.field), (remote.BuildMismatch, "protocol"))
        self.assertEqual(rebuilt.hub_build, remote.tree_digest(HERE / "sd_db"))


class TheNightly(Case):
    """`sd_db.satellite --apply` installs on a mismatch; a dry run plans it."""

    def run_stage(self, refused, *, apply: bool) -> list[str]:
        home = self.root / "home"
        config = home / ".config/sd/hub.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"hub": "hub.example.test", "port": 8769}))
        out = io.StringIO()
        with mock.patch.object(remote, "connect", side_effect=refused), \
                mock.patch.object(self_install, "pip_install", self.unpack), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self_install.OFF, None)
            code = satellite.run("hub.example.test", 8769, apply=apply, home=home, out=out,
                                 source=self.origin.source, venv=self.venv)
        self.assertEqual(code, 0)
        return out.getvalue().splitlines()

    def test_a_dry_run_plans_the_install_and_installs_nothing(self):
        wanted = self.origin.digest()
        lines = self.run_stage(mismatch(hub_value=wanted, hub_build=wanted), apply=False)
        self.assertIn(f"  [dry-run] install the hub's sd_db build {wanted} from origin/main of "
                      f"{self.origin.source}, if its digest is the hub's", lines)
        self.assertEqual(self.unpack.calls, [])

    def test_apply_installs_the_hubs_build(self):
        wanted = self.origin.digest()
        lines = self.run_stage(mismatch(hub_value=wanted, hub_build=wanted), apply=True)
        self.assertEqual(self.installed(), wanted)
        self.assertTrue(any(line.startswith("  ok      installed") for line in lines), lines)

    def test_apply_names_why_it_did_not_install(self):
        lines = self.run_stage(mismatch(hub_value="0" * 16, hub_build="0" * 16), apply=True)
        self.assertEqual(self.unpack.calls, [])
        self.assertTrue(any(line.startswith("  DIFFERS") and "origin/main" in line and "0" * 16 in line
                            for line in lines), lines)


class TheInstallVerb(unittest.TestCase):
    """`sd-db.sh install VENV` records the checkout it installed from."""

    def test_install_writes_the_source_marker_into_the_venv(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        target = root / "venv"
        (target / "bin").mkdir(parents=True)
        log = root / "pip.log"
        stub = target / "bin" / "python"
        stub.write_text(f'#!/bin/sh\necho "$@" > "{log}"\n')
        stub.chmod(0o755)
        done = subprocess.run(["sh", str(HERE / "sd-db.sh"), "install", str(target)], capture_output=True,
                              check=False, text=True, timeout=120, env={**os.environ, "PYTHON": sys.executable})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("-m pip install", log.read_text())
        self.assertEqual((target / self_install.MARKER).read_text().strip(), str(HERE.parent.resolve()))


if __name__ == "__main__":
    unittest.main()
