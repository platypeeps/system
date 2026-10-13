"""The backbone knows the new pieces: criterion 22 of
docs/work/2026-09-05-one-database-one-front-door, the half this repository
can assert.

Every case runs machine-setup.sh against a fixture home with the state
directory pointed inside it, so nothing here reads the machine's profile,
its LaunchAgents or its database. `launchctl`, `tailscale` and `curl` are
stubs on PATH ahead of the real ones, driven by environment variables; the
stub `python` stands in for a virtualenv interpreter: it prints what the
version report asks a real one for. sqlite3, awk and sed are the real binaries, because the
checks are what they do.

The suite is Python for the reason local-repo-sync's is: the CI wrapper asserts a unittest summary and refuses skips.
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

from tests import fixture_config, plist_tools

# The fixture database is built by the library, never by SQL here: only
# sd_db opens the database and only its migration files create tables
# (tests/test_one_store.py greps the repository for both).
# machine-setup.sh test puts the command pack's lib/ on PYTHONPATH (sd:3279).
import sd_db
from sd_db.database import set_schema_version
from sd_db.migrate import initialise
from sd_db.schema import SCHEMA_VERSION

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
ROOT = FOLDER.parent
# The command pack checkout whose lib/ holds sd_db, as machine-setup.sh names it.
PACK = pathlib.Path(os.environ.get("SD_PACK_ROOT") or pathlib.Path.home() / "repos/platypeeps/sd-ai-command-pack")
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


def manifest(name):
    lines = (PROFILES / name).read_text().splitlines()
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def checkout_schema_version():
    # What doctor reads out of the pack's lib/sd_db/schema.py with sed is the
    # same constant this suite imports; assert that, so a doctor reading
    # the wrong file cannot agree with a test reading the right one.
    text = (PACK / "lib/sd_db/schema.py").read_text()
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
        write_stub(self.stubs, "launchctl", 'exit 0\n')
        write_stub(self.stubs, "tailscale",
                   'case "$1 $2" in "serve status") cat "$STUB_SERVE_STATUS" ;; esac\nexit 0\n')
        # curl -sS -m 10 -o FILE -w '%{http_code}' URL: write the body to
        # FILE and print the code; 000 is curl failing to connect.
        write_stub(self.stubs, "curl",
                   'out=""\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                   '[ "${STUB_CURL_CODE:-200}" = 000 ] && { echo "curl: (7) Failed to connect" >&2; exit 7; }\n'
                   'printf "%s" "$STUB_CURL_BODY" > "$out"\nprintf "%s" "${STUB_CURL_CODE:-200}"\n')
        # The version report runs `python -I -c ...` and reads back the file
        # sd_db resolved to. Every other question is answered for real: `-I`
        # dropped so the pack's lib/ on PYTHONPATH is the library.
        self.python = write_stub(self.stubs, "python",
                                 'case "$*" in *sd_db.__file__*) [ -z "$STUB_SD_DB_FILE" ] || { printf "%s\\n" "$STUB_SD_DB_FILE"; exit 0; } ;; esac\n'
                                 '[ "$1" = -I ] && shift\n'
                                 'PYTHONPATH="$STUB_PYTHONPATH" exec "$STUB_REAL_PYTHON" "$@"\n')
        # A pack checkout with the real lib/ and no .venv: doctor reads its
        # schema, and its absent interpreter is reported, not failed.
        self.pack = self.home / "pack"
        self.pack.mkdir()
        (self.pack / "lib").symlink_to(PACK / "lib")
        self.db = self.home / ".local/share/sd/sd.db"
        # Where the pack's installer seeds the provider registry; unwritten here.
        self.registry = self.home / ".local/share/sd/providers.yaml"
        self.serve_status = pathlib.Path(self.tmp.name) / "serve-status"
        self.serve_status.write_text(ROUTE_PRESENT)

    def destroy(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "SD_PACK_ROOT": str(self.pack),
            "SD_DASHBOARD_PYTHON": str(self.python),
            "STUB_REAL_PYTHON": sys.executable,
            "STUB_PYTHONPATH": str(PACK / "lib"),
            "STUB_SERVE_STATUS": str(self.serve_status),
            "STUB_CURL_BODY": HEALTHY_BODY,
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            **self.plist_env,
        }
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(self, *args, **extra):
        return subprocess.run([str(SCRIPT), *args], env=self.env(**extra), capture_output=True, text=True,
                              cwd=str(self.tmp.name))

    def make_database(self, *, schema=None):
        """`sd-db init` as the library does it. A schema other than the
        current one is set last, through the library, after which the
        library itself would refuse to write the file -- which is the state
        doctor's version report is for."""
        initialise(self.db)
        if schema is not None:
            connection = sd_db.connect(self.db)
            try:
                set_schema_version(connection, schema)
            finally:
                connection.close()

    def write_service_config(self, name, database):
        """`~/.config/sd/<name>.json`, what the tracked plist passes with
        --config, naming a `database`."""
        config = self.home / f".config/sd/{name}.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f'{{"database": "{database}", "port": 8767}}\n')
        return config

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

    def test_personal_agent_names_the_dashboard(self):
        agents = manifest("personal.agent")
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
        self.assertRegex(out, r"\[dry-run\] \S+/bin/sd-db init")
        self.assertIn("MISSING tailscale serve https:8443 -> 127.0.0.1:8767", out)
        self.assertIn("[dry-run] tailscale serve --bg --https=8443 --set-path=/ http://127.0.0.1:8767", out)
        self.assertFalse(self.fixture.db.exists(), "a dry run created the database")

    def test_setup_lists_the_dashboard_agent_as_an_action(self):
        out = self.stage_lines("setup", "agents")
        label = "local.system-tools.sd-dashboard"
        self.assertIn(f"MISSING {label}", out)
        self.assertRegex(out, rf"\[dry-run\] cp \S+/{re.escape(label)}\.plist")

    def test_status_counts_all_three_as_drift(self):
        # status runs each stage the way status_stage does and greps the
        # marker words; the two stages that hold the three pieces are run the
        # same way here so brew, repos and the App Store stay out of a test.
        out = self.stage_lines("update", "sd") + self.stage_lines("update", "agents")
        markers = [line for line in out.splitlines() if DRIFT_WORDS.search(line)]
        named = "\n".join(markers)
        for piece in ("database", "tailscale serve", "local.system-tools.sd-dashboard"):
            self.assertIn(piece, named, named)
        self.assertGreaterEqual(len(markers), 3, named)

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
        # status walks STAGES too (sd:3548), so it keeps the same order.
        text = SCRIPT.read_text()
        stages = re.search(r'^STAGES="([^"]*)"', text, re.M).group(1).split()
        self.assertLess(stages.index("sd"), stages.index("agents"), stages)
        self.assertRegex(text, r"(?m)^  for st in \$STAGES; do$")

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
        config = self.fixture.write_service_config("dashboard", "/elsewhere/sd.db")
        out = self.stage_lines("update", "sd")
        self.assertIn(f"DIFFERS database: {config} names /elsewhere/sd.db, not {self.fixture.db}", out)

    def test_a_service_config_naming_this_database_is_not_drift(self):
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_PRESENT)
        self.fixture.write_service_config("dashboard", self.fixture.db)
        out = self.stage_lines("update", "sd")
        self.assertNotRegex(out, DRIFT_WORDS)

    def test_sd_db_path_does_not_move_the_database(self):
        # Nothing else honours it: sd-db init, the library and
        # the dashboard all use the default. Moving only this stage's view
        # checked a file no agent opened.
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_PRESENT)
        out = self.stage_lines("update", "sd", SD_DB_PATH="/elsewhere/sd.db")
        self.assertIn(f"ok      database {self.fixture.db}", out)
        self.assertNotIn("/elsewhere", out)


class DoctorTest(unittest.TestCase):
    """doctor names a corrupted database and a missing route, each with stubbed launchctl, tailscale
    and curl, and reports each virtualenv's sd_db beside the database."""

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.destroy)
        fixture_config.seal(self, self.fixture.stubs)

    def doctor(self, **extra):
        result = self.fixture.run("doctor", "sd", **extra)
        return result.returncode, result.stdout + result.stderr

    def test_all_sound_is_no_hard_failure(self):
        self.fixture.make_database()
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("ok      database", out)
        self.assertIn("passes integrity_check", out)
        self.assertIn("ok      dashboard route https://host.example-tailnet.invalid:8443 -> 127.0.0.1:8767", out)
        self.assertIn("ok      dashboard answers over https://host.example-tailnet.invalid:8443/health", out)
        self.assertNotIn("FAIL", out)

    def test_a_corrupted_database_is_named(self):
        self.fixture.make_database()
        self.fixture.corrupt_database()
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL    database \S+/sd\.db integrity_check: ")
        self.assertIn("sd-db restore", out)

    def test_a_missing_database_is_named(self):
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"FAIL    database MISSING at \S+/sd\.db")

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
        # find it is running on a broken PATH, and the database
        # went unchecked -- which must not read as "no hard failures".
        self.fixture.make_database()
        code, out = self.doctor(PATH=self.fixture.path_without_sqlite3())
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    sqlite3 MISSING from PATH — database not checked", out)
        self.assertIn("/usr/bin/sqlite3", out)
        self.assertNotIn("SKIP    sqlite3", out)

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

    def test_the_version_report_reads_the_packs_lib(self):
        """sd:3279: every sd_db caller runs the pack's lib/, so doctor reads its schema there."""
        self.fixture.make_database()
        schema = checkout_schema_version()
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(f"sd_db in the pack's lib/ {self.fixture.pack}/lib: built for schema {schema}; database at schema {schema}", out)
        self.assertRegex(out, rf"ok      \S+/python imports sd_db from {re.escape(str(self.fixture.pack))}/lib")
        self.assertIn(f"no interpreter at {self.fixture.pack}/.venv/bin/python", out)  # reported, not failed

    def test_a_database_behind_the_packs_lib_fails_and_names_its_migrate(self):
        schema = checkout_schema_version()
        self.fixture.make_database(schema=schema - 1)
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    database schema {schema - 1} DIFFERS from the pack's lib/ {schema}", out)
        self.assertIn(f"{self.fixture.pack}/bin/sd-db migrate", out)
        self.assertIn("sd-serve", out)  # sd:2974: the hub's listener stops too

    def test_an_interpreter_resolving_another_copy_is_named(self):
        # An installed copy, a pack-built one included, or any other folder's sd_db.
        self.fixture.make_database()
        for elsewhere in ("/venv/lib/site-packages/sd_db/__init__.py", f"{ROOT}/elsewhere/sd_db/__init__.py"):
            with self.subTest(elsewhere=elsewhere):
                code, out = self.doctor(STUB_SD_DB_FILE=elsewhere)
                self.assertEqual(code, 1, out)
                self.assertRegex(out, rf"FAIL    \S+/python resolves sd_db from {re.escape(elsewhere)}, not the pack's lib/")

    def test_a_named_interpreter_that_is_missing_fails(self):
        # A failed probe is not a pass: an interpreter the operator named must exist.
        self.fixture.make_database()
        missing = self.fixture.home / "no-python"
        code, out = self.doctor(SD_DASHBOARD_PYTHON=missing)
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    no interpreter at {missing}, which SD_DASHBOARD_PYTHON names", out)

    def test_an_sd_serve_plist_that_picks_a_library_fails(self):
        # An older or hand-edited plist can set SD_DB_LIBRARY, which the pack's
        # sd-db ignores (the 2026-10-10 incident, sd:3281).
        self.fixture.make_database()
        agents = self.fixture.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "local.system-tools.sd-serve.plist").write_bytes(plistlib.dumps(
            {"Label": "local.system-tools.sd-serve", "EnvironmentVariables": {"SD_DB_LIBRARY": "installed"}}))
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL    local.system-tools.sd-serve sets SD_DB_LIBRARY=installed", out)

    def test_a_pack_without_lib_fails_and_names_the_move(self):
        self.fixture.make_database()
        code, out = self.doctor(SD_PACK_ROOT=self.fixture.home / "no-pack")
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    no sd_db at {self.fixture.home}/no-pack/lib", out)
        self.assertIn("repo-sync.sh refresh", out)


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

    def test_a_profile_without_sd_agents_skips_every_check(self):
        (self.fixture.state / "profile").write_text("work\n")
        code, out = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("SKIP    sd pieces not checked", out)
        self.assertNotIn("FAIL", out)
        # A machine with no sd agent, a satellite among them, still passes the
        # retired-folder check when no config names it.
        self.assertIn("ok      no config names the retired local-sd-db", out)

    # sd:3281: a missed machine shows up here, not as a job that runs a missing script.

    def config_copy(self):
        return fixture_config.copy_config(self.fixture.home / "copy")

    def test_an_installed_plist_naming_the_retired_folder_fails(self):
        self.fixture.make_database()
        agents = self.fixture.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        plist = agents / "local.system-tools.sd-serve.plist"
        plist.write_bytes(plistlib.dumps({"Label": "local.system-tools.sd-serve", "ProgramArguments": [
            "/home/someone/repos/system/local-sd-db/sd-db", "serve"]}))
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    {plist} names the retired local-sd-db, now only a forwarder to be deleted", out)
        self.assertIn("bin/sd-db", out)

    def test_a_cron_job_file_naming_the_retired_folder_fails(self):
        self.fixture.make_database()
        config = self.config_copy()
        job = config / "cron-jobs/jobs/sd-db-backup.job"
        job.parent.mkdir(parents=True)
        job.write_text('JOB_COMMAND="\\"$ROOT/../local-sd-db/sd-db\\" backup"\n')
        code, out = self.doctor(SYSTEM_TOOLS_CONFIG=config)
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    {job} names the retired local-sd-db, now only a forwarder to be deleted", out)

    def test_an_installed_cron_plist_naming_the_retired_folder_fails(self):
        self.fixture.make_database()
        agents = self.fixture.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        plist = agents / "local.system-tools.cron.credentials-nightly.plist"
        plist.write_bytes(plistlib.dumps({"Label": "local.system-tools.cron.credentials-nightly", "ProgramArguments": [
            "/bin/sh", "-c", "/home/someone/repos/system/local-sd-db/sd-db.sh credentials"]}))
        code, out = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    {plist} names the retired local-sd-db", out)

    def test_a_per_host_cron_job_file_naming_the_retired_folder_fails(self):
        self.fixture.make_database()
        config = self.config_copy()
        job = config / "cron-jobs/jobs/host-a/credentials-nightly.job"
        job.parent.mkdir(parents=True)
        job.write_text('JOB_COMMAND="\\"$ROOT/../local-sd-db/sd-db.sh\\" credentials"\n')
        code, out = self.doctor(SYSTEM_TOOLS_CONFIG=config)
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    {job} names the retired local-sd-db", out)

    def test_a_plist_template_naming_the_retired_folder_fails(self):
        self.fixture.make_database()
        config = self.config_copy()
        template = config / "machine-setup/launchagents/local.system-tools.sd-serve.plist"
        template.write_text(template.read_text().replace(
            "@HOME@/repos/platypeeps/sd-ai-command-pack/bin/sd-db", "@ROOT@/local-sd-db/sd-db"))
        code, out = self.doctor(SYSTEM_TOOLS_CONFIG=config)
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    {template} names the retired local-sd-db, now only a forwarder to be deleted", out)

    def test_a_job_folder_that_cannot_be_searched_is_not_a_pass(self):
        self.fixture.make_database()
        config = self.config_copy()
        jobs = config / "cron-jobs/jobs"
        jobs.mkdir(parents=True)
        (jobs / "a.job").write_text("JOB_COMMAND=true\n")
        jobs.chmod(0)
        self.addCleanup(jobs.chmod, 0o755)
        code, out = self.doctor(SYSTEM_TOOLS_CONFIG=config)
        self.assertEqual(code, 1, out)
        self.assertIn(f"FAIL    cannot search {jobs} for config that names the retired local-sd-db", out)
        self.assertNotIn("ok      no config names", out)


if __name__ == "__main__":
    unittest.main()
