"""The backbone knows the new pieces: criterion 22 of
docs/work/2026-09-05-one-database-one-front-door, the half this repository
can assert.

Every case runs machine-setup.sh against a fixture home with the state
directory pointed inside it, so nothing here reads the machine's profile,
its LaunchAgents or its database. `launchctl`, `tailscale` and `curl` are
stubs on PATH ahead of the real ones, driven by environment variables; the
stub `python` stands in for a virtualenv interpreter: it prints what the
version report asks a real one for, and answers every other question with
this checkout's library, the way the runner's interpreter answers with the
installed copy. sqlite3, awk and sed are the real binaries, because the
checks are what they do.

The suite is Python for the reason local-repo-sync's and local-sd-plan's
are: the CI wrapper asserts a unittest summary and refuses skips.
"""

import os
import pathlib
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from tests import fixture_config, plist_tools
from unittest import mock

# The fixture database is built by the library, never by SQL here: only
# sd_db opens the database and only its migration files create tables
# (local-sd-db/tests/test_one_store.py greps the repository for both).
# machine-setup.sh test puts this checkout's local-sd-db on PYTHONPATH, the
# way local-sd-plan and local-sd-runner do.
import sd_db
from sd_db import provider_controls
from sd_db import runner as sd_runner
from sd_db.database import set_schema_version
from sd_db.migrate import initialise
from sd_db.schema import SCHEMA_VERSION

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
ROOT = FOLDER.parent
SCRIPT = FOLDER / "machine-setup.sh"
PROFILES = fixture_config.PROFILES
AGENTS = fixture_config.AGENTS

DRIFT_WORDS = re.compile(r"DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA")

ROUTE_PRESENT = (
    "https://host.example-tailnet.invalid (Funnel on)\n"
    "|-- / proxy http://127.0.0.1:8766\n"
    "\n"
    "https://host.example-tailnet.invalid:8443 (tailnet only)\n"
    "|-- / proxy http://127.0.0.1:8767\n"
)
ROUTE_ABSENT = (
    "https://host.example-tailnet.invalid (Funnel on)\n"
    "|-- / proxy http://127.0.0.1:8766\n"
)
# The route is there and points at the dashboard, but its authority is public:
# the private front door on the internet behind a login header (sd:1413).
ROUTE_UNDER_FUNNEL = (
    "https://host.example-tailnet.invalid (Funnel on)\n"
    "|-- / proxy http://127.0.0.1:8766\n"
    "\n"
    "https://host.example-tailnet.invalid:8443 (Funnel on)\n"
    "|-- / proxy http://127.0.0.1:8767\n"
)
# The dashboard's port serving something that is not the dashboard's route:
# another backend, a target that only contains 8767, and the right target on
# a path other than the root. None of them is the route, and each is a port
# somebody else holds.
ROUTE_HELD_BY_ANOTHER_BACKEND = (
    "https://host.example-tailnet.invalid:8443 (tailnet only)\n"
    "|-- / proxy http://127.0.0.1:9999\n"
)
ROUTE_TARGET_CONTAINS_THE_PORT = (
    "https://host.example-tailnet.invalid:8443 (tailnet only)\n"
    "|-- / proxy http://127.0.0.1:87670\n"
)
ROUTE_NOT_AT_THE_ROOT = (
    "https://host.example-tailnet.invalid:8443 (tailnet only)\n"
    "|-- /other proxy http://127.0.0.1:8767\n"
)
HEALTHY_BODY = '{"service": "sd-dashboard", "ok": true, "pid": 4711}'
# A registry the library accepts: two enabled entries, one naming a variable
# and one naming none, a disabled entry naming a variable doctor must not
# ask about, and a fourth so each role list still resolves to a different
# entry after `one` is switched off through a row.
FIXTURE_REGISTRY = """\
bills:
  fixture: { cost: subscription }
providers:
  one:   { start: "one exec",   vendor: fixture, bill: fixture, roles: [author], env: [FIXTURE_KEY_ONE] }
  two:   { start: "two exec",   vendor: fixture, bill: fixture, roles: [author, reviewer], env: [] }
  three: { start: "three exec", vendor: fixture, bill: fixture, roles: [reviewer],
           enabled: false, reason: "fixture", env: [FIXTURE_KEY_TWO] }
  four:  { start: "four exec",  vendor: fixture, bill: fixture, roles: [reviewer], env: [] }
roles:
  author:   [one, two]
  reviewer: [four, two, three]
"""
FIXTURE_VALUE = "hunter2-fixture-value"


def manifest(name):
    lines = (PROFILES / name).read_text().splitlines()
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def checkout_schema_version():
    # What doctor reads out of local-sd-db/sd_db/schema.py with sed is the
    # same constant this suite imports; assert that, so a doctor reading
    # the wrong file cannot agree with a test reading the right one.
    text = (ROOT / "local-sd-db/sd_db/schema.py").read_text()
    from_file = int(re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", text, re.M).group(1))
    assert from_file == SCHEMA_VERSION, (from_file, SCHEMA_VERSION)
    return SCHEMA_VERSION


def write_stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Fixture:
    """A home with a recorded `personal` profile and nothing else."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = pathlib.Path(self.tmp.name) / "stubs"
        self.stubs.mkdir()
        # The sd stage reads each service config's `database` with plutil,
        # which a Linux runner does not have.
        self.plist_env = plist_tools.install(self.stubs)
        write_stub(self.stubs, "launchctl",
                   'case "$*" in *local.system-tools.sd-runner*) exit "${STUB_RUNNER_LOADED:-0}" ;; esac\nexit 0\n')
        write_stub(self.stubs, "tailscale",
                   'case "$1 $2" in "serve status") cat "$STUB_SERVE_STATUS" ;; esac\nexit 0\n')
        # curl -sS -m 10 -o FILE -w '%{http_code}' URL: write the body to
        # FILE and print the code; 000 is curl failing to connect.
        write_stub(self.stubs, "curl",
                   'out=""\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                   '[ "${STUB_CURL_CODE:-200}" = 000 ] && { echo "curl: (7) Failed to connect" >&2; exit 7; }\n'
                   'printf "%s" "$STUB_CURL_BODY" > "$out"\nprintf "%s" "${STUB_CURL_CODE:-200}"\n')
        # The version report runs `python -I -c ...` and reads one line back:
        # dist version, schema version, the file sd_db resolved to. Doctor's
        # provider check asks the same interpreter for the registry through
        # sd_db, and that question is answered for real: `-I` dropped so
        # this checkout's local-sd-db on PYTHONPATH is the library.
        self.python = write_stub(self.stubs, "python",
                                 'case "$*" in *importlib.metadata*) printf "%s\\n" "$STUB_SD_DB_INFO"; exit 0 ;; esac\n'
                                 '[ "$1" = -I ] && shift\n'
                                 # A HOME the library would default to, set
                                 # apart from doctor's, so a read that relies
                                 # on the default opens the wrong files.
                                 '[ -n "$STUB_PYTHON_HOME" ] && export HOME="$STUB_PYTHON_HOME"\n'
                                 'PYTHONPATH="$STUB_PYTHONPATH" exec "$STUB_REAL_PYTHON" "$@"\n')
        self.db = self.home / ".local/share/sd/sd.db"
        self.registry = self.home / ".local/share/sd/providers.yaml"
        self.shell_env = self.home / ".config/shell/env.sh"
        # A sound machine: the registry the pack's installer seeds, and the
        # one variable its enabled entries name exported where runner.sh
        # sources it. Tests that want the gap overwrite one or the other.
        self.write_registry()
        self.write_shell_env(f"export FIXTURE_KEY_ONE={FIXTURE_VALUE}\n")
        self.serve_status = pathlib.Path(self.tmp.name) / "serve-status"
        self.serve_status.write_text(ROUTE_PRESENT)

    def destroy(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "SD_PACK_ROOT": str(self.home / "no-pack"),
            "SD_DASHBOARD_PYTHON": str(self.python),
            "SD_RUNNER_PYTHON": str(self.python),
            "STUB_REAL_PYTHON": sys.executable,
            "STUB_PYTHONPATH": str(ROOT / "local-sd-db"),
            "STUB_SERVE_STATUS": str(self.serve_status),
            "STUB_CURL_BODY": HEALTHY_BODY,
            "STUB_SD_DB_INFO": f"0.1.0 {checkout_schema_version()} /venv/lib/site-packages/sd_db/__init__.py",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            **self.plist_env,
        }
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(self, *args, **extra):
        return subprocess.run([str(SCRIPT), *args], env=self.env(**extra), capture_output=True, text=True,
                              cwd=str(self.tmp.name))

    def make_database(self, *, schema=None, heartbeat_age=timedelta(seconds=5), healthy=True):
        """`sd-db.sh init` as the library does it, then the runner's own
        heartbeat write, with its clock moved back by `heartbeat_age`. A
        schema other than the current one is set last, through the library,
        after which the library itself would refuse to write the file --
        which is the state doctor's version report is for."""
        initialise(self.db)
        connection = sd_db.connect(self.db)
        try:
            if heartbeat_age is not None:
                stamp = (datetime.now(UTC) - heartbeat_age).isoformat(timespec="seconds")
                with mock.patch.object(sd_runner, "now", return_value=stamp):
                    sd_runner.heartbeat(connection, {"healthy": healthy, "interval_seconds": 10, "pid": 1457})
            if schema is not None:
                set_schema_version(connection, schema)
        finally:
            connection.close()

    def write_registry(self, text=FIXTURE_REGISTRY):
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(text)

    def write_shell_env(self, text):
        """`~/.config/shell/env.sh`, the file runner.sh sources on serve."""
        self.shell_env.parent.mkdir(parents=True, exist_ok=True)
        self.shell_env.write_text(text)

    def write_service_config(self, name, database):
        """`~/.config/sd/<name>.json`, what the tracked plist passes with
        --config, naming a `database`."""
        config = self.home / f".config/sd/{name}.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f'{{"database": "{database}", "port": 8767}}\n')
        return config

    def disable_provider(self, name):
        """Switch an entry off the way the dashboard does: a row, through
        the library, with the file still saying it is enabled."""
        # The registry path is passed, never defaulted: the library reads
        # `$HOME` at call time and this process's HOME is the real one.
        connection = sd_db.connect(self.db)
        try:
            current = provider_controls.snapshot(connection, path=self.registry)
            enabled = {entry["name"]: entry["enabled"] for entry in current["providers"]}
            enabled[name] = False
            provider_controls.configure(connection, enabled=enabled, orders=current["orders"],
                                        expected_revision=current["revision"], path=self.registry, who="operator")
        finally:
            connection.close()

    def path_without_sqlite3(self):
        """A PATH holding the stubs and every system command but sqlite3.

        A stub cannot hide a binary: `command -v` skips a non-executable
        file and keeps searching. So the system directories are mirrored as
        symlinks with sqlite3 left out, and PATH names the mirror instead.
        """
        mirror = pathlib.Path(self.tmp.name) / "no-sqlite3"
        mirror.mkdir()
        for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            for entry in sorted(pathlib.Path(directory).iterdir()):
                if entry.name == "sqlite3" or (mirror / entry.name).exists():
                    continue
                (mirror / entry.name).symlink_to(entry)
        return f"{self.stubs}:{mirror}"

    def corrupt_database(self):
        # Bytes into the file, never SQL. Page 2 holds the first table's
        # root and integrity_check walks the tree, so its header is what is
        # overwritten; a byte inside a cell body would pass.
        with open(self.db, "r+b") as handle:
            handle.seek(4096)
            handle.write(b"\xff" * 512)


class ProfileTest(unittest.TestCase):
    """The profile names the pieces; the tracked plist execs the new server."""

    def test_personal_agent_names_both_sd_agents(self):
        agents = manifest("personal.agent")
        self.assertIn("local.system-tools.sd-runner", agents)
        self.assertIn("local.system-tools.sd-dashboard", agents)

    def test_tracked_dashboard_plist_execs_the_dashboard_server_on_8767(self):
        for template in (AGENTS / "local.system-tools.sd-dashboard.plist",
                         FOLDER / "examples/launchagents/local.system-tools.sd-dashboard.plist"):
            with self.subTest(template=template.relative_to(FOLDER.parent)):
                label = template.stem
                plist = plistlib.loads(fixture_config.render(template, label, "/home/someone", ROOT).encode())
                self.assertEqual(plist["Label"], label)
                arguments = plist["ProgramArguments"]
                self.assertEqual(arguments[0], f"{ROOT}/local-project-dashboard/dashboard.sh")
                self.assertEqual(arguments[1], "serve")
                self.assertEqual(arguments[arguments.index("--port") + 1], "8767")
                self.assertNotIn("sd-ai-command-pack/bin/sd-dashboard", arguments[0])
                # KeepAlive without a throttle is a crash loop; every KeepAlive agent
                # here sets both.
                self.assertTrue(plist["KeepAlive"])
                self.assertEqual(plist["ThrottleInterval"], 30)


class SetupAndStatusTest(unittest.TestCase):
    """A home with none of the four pieces: setup lists the actions, the
    stages name each gap with a word the drift counter greps for."""

    def setUp(self):
        self.fixture = Fixture()
        self.fixture.serve_status.write_text(ROUTE_ABSENT)
        self.addCleanup(self.fixture.destroy)
        fixture_config.seal(self, self.fixture.stubs)

    def stage_lines(self, verb, stage, **extra):
        result = self.fixture.run(verb, *(["personal"] if verb == "setup" else []), stage, **extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_setup_lists_the_database_init_and_the_route_as_actions(self):
        out = self.stage_lines("setup", "sd")
        self.assertIn("MISSING database", out)
        self.assertRegex(out, r"\[dry-run\] \S+/local-sd-db/sd-db\.sh init")
        self.assertIn("MISSING tailscale serve https:8443 -> 127.0.0.1:8767", out)
        self.assertIn("[dry-run] tailscale serve --bg --https=8443 --set-path=/ http://127.0.0.1:8767", out)
        self.assertFalse(self.fixture.db.exists(), "a dry run created the database")

    def test_setup_lists_both_agents_as_actions(self):
        out = self.stage_lines("setup", "agents")
        for label in ("local.system-tools.sd-dashboard", "local.system-tools.sd-runner"):
            self.assertIn(f"MISSING {label}", out)
            self.assertRegex(out, rf"\[dry-run\] cp \S+/{re.escape(label)}\.plist")

    def test_status_counts_all_four_as_drift(self):
        # status runs each stage the way status_stage does and greps the
        # marker words; the two stages that hold the four pieces are run the
        # same way here so brew, repos and the App Store stay out of a test.
        out = self.stage_lines("update", "sd") + self.stage_lines("update", "agents")
        markers = [line for line in out.splitlines() if DRIFT_WORDS.search(line)]
        named = "\n".join(markers)
        for piece in ("database", "tailscale serve", "local.system-tools.sd-dashboard", "local.system-tools.sd-runner"):
            self.assertIn(piece, named, named)
        self.assertGreaterEqual(len(markers), 4, named)

    def test_a_home_with_the_pieces_reports_no_drift_on_them(self):
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_PRESENT)
        out = self.stage_lines("update", "sd")
        self.assertNotRegex(out, DRIFT_WORDS)
        self.assertIn("ok      database", out)
        self.assertIn("ok      tailscale serve https://host.example-tailnet.invalid:8443 -> 127.0.0.1:8767", out)

    def test_a_profile_without_sd_agents_skips(self):
        (self.fixture.state / "profile").write_text("work\n")
        out = self.stage_lines("update", "sd")
        self.assertIn("SKIP    no sd agent in this profile", out)
        self.assertNotRegex(out, DRIFT_WORDS)

    # sd:1177, the review findings on #280.

    def test_setup_builds_the_database_before_it_loads_the_agents(self):
        # Both agents open the database at startup, so `agents` running
        # first bootstraps them into a restart loop on a fresh machine.
        # status walks its own list; it keeps the same order.
        text = SCRIPT.read_text()
        stages = re.search(r'^STAGES="([^"]*)"', text, re.M).group(1).split()
        self.assertLess(stages.index("sd"), stages.index("agents"), stages)
        status = re.search(r"^  for st in ([^;]*); do$", text, re.M).group(1).split()
        self.assertLess(status.index("sd"), status.index("agents"), status)

    def test_a_port_held_by_another_route_is_named_and_not_replaced(self):
        self.fixture.make_database()
        for status in (ROUTE_HELD_BY_ANOTHER_BACKEND, ROUTE_TARGET_CONTAINS_THE_PORT, ROUTE_NOT_AT_THE_ROOT):
            with self.subTest(status=status):
                self.fixture.serve_status.write_text(status)
                out = self.stage_lines("update", "sd")
                self.assertIn("DIFFERS tailscale serve https:8443 already serves https://host.example-tailnet.invalid:8443", out)
                self.assertNotIn("ok      tailscale serve", out)
                self.assertNotIn("[dry-run] tailscale serve", out)

    def test_a_service_config_naming_another_database_is_drift(self):
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_PRESENT)
        config = self.fixture.write_service_config("runner", "/elsewhere/sd.db")
        out = self.stage_lines("update", "sd")
        self.assertIn(f"DIFFERS database: {config} names /elsewhere/sd.db, not {self.fixture.db}", out)

    def test_a_service_config_naming_this_database_is_not_drift(self):
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_PRESENT)
        self.fixture.write_service_config("runner", self.fixture.db)
        self.fixture.write_service_config("dashboard", self.fixture.db)
        out = self.stage_lines("update", "sd")
        self.assertNotRegex(out, DRIFT_WORDS)

    def test_sd_db_path_does_not_move_the_database(self):
        # Nothing else honours it: sd-db.sh init, the library, the runner and
        # the dashboard all use the default. Moving only this stage's view
        # checked a file no agent opened.
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_PRESENT)
        out = self.stage_lines("update", "sd", SD_DB_PATH="/elsewhere/sd.db")
        self.assertIn(f"ok      database {self.fixture.db}", out)
        self.assertNotIn("/elsewhere", out)


class DoctorTest(unittest.TestCase):
    """doctor names a corrupted database, an unloaded runner, a stale
    heartbeat and a missing route, each with stubbed launchctl, tailscale
    and curl, and reports each virtualenv's sd_db beside the database."""

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.destroy)
        fixture_config.seal(self, self.fixture.stubs)

    def doctor(self, **extra):
        result = self.fixture.run("doctor", "sd", **extra)
        return result.returncode, result.stdout + result.stderr

    def test_all_four_sound_is_no_hard_failure(self):
        self.fixture.make_database()
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("ok      database", out)
        self.assertIn("passes integrity_check", out)
        self.assertRegex(out, r"ok      runner heartbeat \d+s old \(interval 10s\)")
        self.assertIn("ok      dashboard route https://host.example-tailnet.invalid:8443 -> 127.0.0.1:8767", out)
        self.assertIn("ok      dashboard answers over https://host.example-tailnet.invalid:8443/health", out)
        self.assertNotIn("FAIL", out)

    def test_a_corrupted_database_is_named(self):
        self.fixture.make_database()
        self.fixture.corrupt_database()
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL    database \S+/sd\.db integrity_check: ")
        self.assertIn("sd-db.sh restore", out)

    def test_a_missing_database_is_named(self):
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL    database MISSING at \S+/sd\.db")
        self.assertIn("runner heartbeat not checked", out)

    def test_an_unloaded_runner_is_named(self):
        self.fixture.make_database()
        code, out = self.doctor(STUB_RUNNER_LOADED=1)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    local.system-tools.sd-runner UNLOADED", out)

    def test_a_stale_heartbeat_is_named(self):
        self.fixture.make_database(heartbeat_age=timedelta(minutes=10))
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL    runner heartbeat STALE — \d+s old, fresh is within 30s")

    def test_an_unhealthy_heartbeat_is_named(self):
        self.fixture.make_database(healthy=False)
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    runner heartbeat reports unhealthy", out)

    def test_a_runner_that_never_wrote_a_heartbeat_is_named(self):
        self.fixture.make_database(heartbeat_age=None)
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    runner heartbeat MISSING", out)

    def test_a_missing_route_is_named_and_the_answer_is_not_asked(self):
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_ABSENT)
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    dashboard route MISSING — no tailscale serve https:8443 -> 127.0.0.1:8767", out)
        self.assertIn("dashboard HTTPS answer not checked", out)

    def test_a_missing_sqlite3_is_a_failure_and_not_a_skip(self):
        # sd:1413. Every profile with an sd agent is a Mac, and macOS ships
        # /usr/bin/sqlite3; no profile installs it. So a doctor that cannot
        # find it is running on a broken PATH, and the database and heartbeat
        # went unchecked -- which must not read as "no hard failures".
        self.fixture.make_database()
        code, out = self.doctor(PATH=self.fixture.path_without_sqlite3())
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    sqlite3 MISSING from PATH — database and runner heartbeat not checked", out)
        self.assertIn("/usr/bin/sqlite3", out)
        self.assertNotIn("SKIP    sqlite3", out)
        self.assertIn("runner heartbeat not checked", out)

    def test_a_dashboard_origin_under_funnel_is_a_failure(self):
        # sd:1413. The front door is tailnet-only; Funnel on its authority is
        # the dashboard on the public internet, which the runtime's own
        # preflight refuses (local-project-dashboard/RUNTIME.md).
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_UNDER_FUNNEL)
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    dashboard origin https://host.example-tailnet.invalid:8443 is PUBLIC under Funnel", out)
        self.assertIn("tailscale serve --bg --https=8443 --set-path=/ off", out)
        self.assertIn("1 hard failure(s)", out)

    def test_funnel_on_another_authority_is_not_the_dashboards(self):
        # task-actions' public 443 sits under Funnel in every fixture; only
        # the dashboard's own :8443 authority counts.
        self.fixture.make_database()
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Funnel", out)

    def test_a_dashboard_that_does_not_answer_is_named(self):
        self.fixture.make_database()
        code, out = self.doctor(STUB_CURL_CODE="000")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    dashboard does not answer over https://host.example-tailnet.invalid:8443", out)

    def test_a_dashboard_that_answers_unhealthy_is_named_with_its_reason(self):
        self.fixture.make_database()
        body = '{"service": "sd-dashboard", "ok": false, "error": "the database is at schema version 6 and this library is built for 5"}'
        code, out = self.doctor(STUB_CURL_BODY=body, STUB_CURL_CODE="503")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    dashboard answers over https://host.example-tailnet.invalid:8443 but reports unhealthy: the database is at schema version 6", out)

    def test_a_local_only_service_behind_the_route_is_a_warning(self):
        self.fixture.make_database()
        code, out = self.doctor(STUB_CURL_BODY='{"error": "Untrusted dashboard host."}', STUB_CURL_CODE="403")
        self.assertEqual(code, 0, out)
        self.assertIn("WARN    dashboard answers over https://host.example-tailnet.invalid:8443 with HTTP 403 (Untrusted dashboard host)", out)

    def test_the_version_report_names_a_virtualenv_built_for_another_schema(self):
        self.fixture.make_database()
        schema = checkout_schema_version()
        code, out = self.doctor(STUB_SD_DB_INFO=f"0.1.0 {schema - 1} /venv/lib/site-packages/sd_db/__init__.py")
        self.assertEqual(code, 1, out)
        self.assertIn(f"sd_db in this checkout: 0.1.0 built for schema {schema}; database at schema {schema}", out)
        self.assertRegex(out, rf"FAIL    \S+/python: sd_db 0\.1\.0 built for schema {schema - 1} DIFFERS from the database's {schema}")
        self.assertIn("no interpreter at", out)  # the absent pack venv is reported, not failed

    def test_the_version_report_names_a_database_behind_the_checkout(self):
        schema = checkout_schema_version()
        self.fixture.make_database(schema=schema - 1)
        code, out = self.doctor(STUB_SD_DB_INFO=f"0.1.0 {schema - 1} /venv/lib/site-packages/sd_db/__init__.py")
        self.assertEqual(code, 0, out)
        self.assertIn(f"WARN    database schema {schema - 1} DIFFERS from this checkout's {schema}", out)
        self.assertIn("sd-db.sh migrate", out)
        # The virtualenv matches the database, so its agents run; it is the
        # checkout that moved ahead, which is a warning and not a failure.
        self.assertRegex(out, rf"WARN    \S+/python: sd_db 0\.1\.0 built for schema {schema - 1} DIFFERS from this checkout's {schema}")
        self.assertNotIn("FAIL", out)

    def test_a_virtualenv_resolving_this_checkout_is_named(self):
        self.fixture.make_database()
        schema = checkout_schema_version()
        code, out = self.doctor(STUB_SD_DB_INFO=f"0.1.0 {schema} {ROOT}/local-sd-db/sd_db/__init__.py")
        self.assertEqual(code, 1, out)
        self.assertIn("resolves sd_db from this checkout", out)

    # Criterion 22's last clause: an enabled entry whose variable is unset is
    # named, and no value is ever printed. The variable is tested where the
    # runner would see it -- after sourcing ~/.config/shell/env.sh -- so the
    # fixture writes that file rather than exporting into doctor's own
    # environment.

    def test_an_enabled_entry_whose_variable_is_unset_is_named_without_a_value(self):
        self.fixture.make_database()
        self.fixture.write_shell_env(f"export FIXTURE_KEY_TWO={FIXTURE_VALUE}\nexport FIXTURE_KEY_ONE=\n")
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL    provider one is enabled and FIXTURE_KEY_ONE is MISSING")
        self.assertIn("1 hard failure(s)", out)
        # The disabled entry's variable is not asked about, and the entries
        # naming no variable raise nothing.
        self.assertNotIn("FIXTURE_KEY_TWO", out)
        self.assertNotIn("provider two", out)
        self.assertNotIn("provider four", out)
        self.assertNotIn(FIXTURE_VALUE, out)

    def test_a_variable_the_runner_would_source_satisfies_the_check_and_stays_secret(self):
        # The fixture's env.sh is the only place the value is set; doctor's
        # own environment never holds it.
        self.fixture.make_database()
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("ok      enabled providers' variables set in the runner's environment: one (FIXTURE_KEY_ONE), two (none), four (none)", out)
        self.assertNotIn("FAIL", out)
        self.assertNotIn(FIXTURE_VALUE, out)

    def test_a_variable_exported_around_doctor_satisfies_the_check_too(self):
        # No env.sh at all: what the caller exported is what a runner run by
        # hand would inherit.
        self.fixture.make_database()
        self.fixture.shell_env.unlink()
        code, out = self.doctor(FIXTURE_KEY_ONE=FIXTURE_VALUE)
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertNotIn(FIXTURE_VALUE, out)

    def test_an_entry_disabled_by_a_row_is_not_asked_about(self):
        # The file says `one` is enabled; the provider row, written the way
        # the dashboard writes it, says it is not. Rows win on state, so the
        # unset variable is nobody's problem -- which is only true if doctor
        # reads the merged registry and not the file.
        self.fixture.make_database()
        self.fixture.shell_env.unlink()
        self.fixture.disable_provider("one")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertNotIn("FIXTURE_KEY_ONE", out)
        self.assertNotIn("FAIL", out)

    def test_a_missing_registry_is_named(self):
        self.fixture.make_database()
        self.fixture.registry.unlink()
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    provider registry MISSING at", out)

    # sd:1177, the review findings on #280 and #286.

    def test_a_port_held_by_another_route_is_a_failure_and_not_the_route(self):
        self.fixture.make_database()
        for status in (ROUTE_HELD_BY_ANOTHER_BACKEND, ROUTE_TARGET_CONTAINS_THE_PORT, ROUTE_NOT_AT_THE_ROOT):
            with self.subTest(status=status):
                self.fixture.serve_status.write_text(status)
                code, out = self.doctor()
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL    dashboard route DIFFERS — https:8443 already serves https://host.example-tailnet.invalid:8443", out)
                self.assertNotIn("ok      dashboard route", out)
                self.assertIn("dashboard HTTPS answer not checked", out)

    def test_an_unhealthy_answer_without_an_error_field_names_its_diagnostics(self):
        # The dashboard's ordinary unhealthy answer: fields, no `error`.
        self.fixture.make_database()
        body = ('{"service": "sd-dashboard", "ok": false, "code_changed": true, "pid": 1, '
                '"schema": 6, "expected_schema": 5, "direct_listener_ok": false}')
        code, out = self.doctor(STUB_CURL_BODY=body, STUB_CURL_CODE="503")
        self.assertEqual(code, 1, out)
        self.assertIn("reports unhealthy: database at schema 6, the server built for 5; "
                      "code changed since the server started, restart local.system-tools.sd-dashboard; "
                      "its direct listener is not healthy", out)

    def test_an_unhealthy_answer_with_no_diagnostics_says_so(self):
        self.fixture.make_database()
        code, out = self.doctor(STUB_CURL_BODY='{"service": "sd-dashboard", "ok": false}', STUB_CURL_CODE="503")
        self.assertEqual(code, 1, out)
        self.assertIn("reports unhealthy: no reason in the health body", out)

    def test_a_service_config_naming_another_database_is_a_failure(self):
        self.fixture.make_database()
        config = self.fixture.write_service_config("dashboard", "/elsewhere/sd.db")
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    database DIFFERS: {config} names /elsewhere/sd.db, not {self.fixture.db}", out)

    def test_the_provider_read_opens_the_database_doctor_checked(self):
        # The row switching `one` off lives in the database doctor checked.
        # The interpreter's HOME points elsewhere, so a read that falls back
        # on the library's default path finds no database and no registry.
        self.fixture.make_database()
        self.fixture.shell_env.unlink()
        self.fixture.disable_provider("one")
        elsewhere = pathlib.Path(self.fixture.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        code, out = self.doctor(STUB_PYTHON_HOME=elsewhere)
        self.assertEqual(code, 0, out)
        self.assertIn("ok      enabled providers' variables set in the runner's environment: two (none), four (none)", out)
        self.assertNotIn("FIXTURE_KEY_ONE", out)

    def test_an_env_file_that_fails_when_sourced_is_a_failure(self):
        # runner.sh sources it under `set -e`; a failing line stops the
        # runner, so the variables set above it do not count.
        self.fixture.make_database()
        for text in (f"export FIXTURE_KEY_ONE={FIXTURE_VALUE}\nfalse\n",
                     f"export FIXTURE_KEY_ONE={FIXTURE_VALUE}\nif then\n"):
            with self.subTest(text=text):
                self.fixture.write_shell_env(text)
                code, out = self.doctor()
                self.assertEqual(code, 1, out)
                self.assertIn(f"FAIL    {self.fixture.shell_env} fails when sourced", out)
                self.assertNotIn("ok      enabled providers' variables", out)
                self.assertNotIn(FIXTURE_VALUE, out)

    def test_a_variable_env_sh_sets_without_exporting_is_missing(self):
        # A bare assignment is a shell variable: the runner's Python process
        # never receives it.
        self.fixture.make_database()
        self.fixture.write_shell_env(f"FIXTURE_KEY_ONE={FIXTURE_VALUE}\n")
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    provider one is enabled and FIXTURE_KEY_ONE is MISSING (unset, empty or not exported)", out)
        self.assertNotIn(FIXTURE_VALUE, out)

    def test_the_runner_interpreter_env_sh_names_is_the_one_asked(self):
        # runner.sh resolves SD_RUNNER_PYTHON after sourcing env.sh; doctor
        # resolved it before, fell back on the pack's absent venv and did
        # not check the variables at all.
        self.fixture.make_database()
        self.fixture.write_shell_env(f"export FIXTURE_KEY_ONE={FIXTURE_VALUE}\n"
                                     f"export SD_RUNNER_PYTHON={self.fixture.python}\n")
        code, out = self.doctor(SD_RUNNER_PYTHON="")
        self.assertEqual(code, 0, out)
        self.assertNotIn("no runner interpreter at", out)
        self.assertIn("ok      enabled providers' variables set in the runner's environment: one (FIXTURE_KEY_ONE)", out)

    def test_the_runner_interpreter_the_installed_plist_names_is_the_one_asked(self):
        # The runner's plist may carry SD_RUNNER_PYTHON in EnvironmentVariables;
        # with neither the environment nor env.sh naming one, that is the
        # interpreter runner.sh starts, and the one doctor must ask.
        self.fixture.make_database()
        label = "local.system-tools.sd-runner"
        plist = plistlib.loads(fixture_config.render(AGENTS / f"{label}.plist", label,
                                                     self.fixture.home, ROOT).encode())
        plist["EnvironmentVariables"] = {"SD_RUNNER_PYTHON": str(self.fixture.python)}
        installed = self.fixture.home / "Library/LaunchAgents" / f"{label}.plist"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(plistlib.dumps(plist))
        code, out = self.doctor(SD_RUNNER_PYTHON="")
        self.assertEqual(code, 0, out)
        self.assertNotIn("no runner interpreter at", out)
        self.assertIn("ok      enabled providers' variables set in the runner's environment: one (FIXTURE_KEY_ONE)", out)

    def test_a_profile_without_sd_agents_skips_every_check(self):
        (self.fixture.state / "profile").write_text("work\n")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("SKIP    sd pieces not checked", out)
        self.assertNotIn("FAIL", out)


if __name__ == "__main__":
    unittest.main()
