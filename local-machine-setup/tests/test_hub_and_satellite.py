"""Steps 8 and 9 of docs/work/2026-09-22-run-the-framework-from-a-second-machine.

Step 8: the hub runs `<prefix>.sd-serve`, and the `sd` stage says when the
profile or the config folder lacks it and when a `hub.json` sits on the hub.
Step 9: the `satellite` stage, criterion 7 and a build mismatch, against a
loopback `sd-db.sh serve` over a database in the case's own folder.

Every case runs machine-setup.sh against a fixture home, with the stubs of
test_backbone on PATH: `launchctl` never reaches the real one, and nothing
here loads, bootstraps or installs an agent on this Mac.
"""

import json
import pathlib
import plistlib
import re
import subprocess
import sys
import time
import unittest

from tests import fixture_config
from tests.test_backbone import DRIFT_WORDS, FOLDER, ROOT, ROUTE_PRESENT, SCRIPT, Fixture, manifest

from sd_db.migrate import initialise

LABEL = f"{fixture_config.LABEL_PREFIX}.sd-serve"
SD_DB_SH = ROOT / "local-sd-db/sd-db.sh"
REGISTRY = """\
bills:
  fixture: { cost: subscription }
providers:
  one: { start: "one exec", vendor: fixture, bill: fixture, roles: [author], env: [] }
  two: { start: "two exec", vendor: fixture, bill: fixture, roles: [reviewer], env: [] }
roles:
  author:   [one]
  reviewer: [two]
"""


def markers(text):
    return [line for line in text.splitlines() if DRIFT_WORDS.search(line)]


class TheServeAgent(unittest.TestCase):
    """The template and the profile line (step 8, gap (f))."""

    def test_personal_agent_names_the_serve_agent(self):
        self.assertIn(LABEL, manifest("personal.agent"))

    def test_the_template_execs_this_checkouts_serve_on_loopback(self):
        for template in (fixture_config.AGENTS / f"{LABEL}.plist",
                         FOLDER / f"examples/launchagents/{LABEL}.plist"):
            with self.subTest(template=template.relative_to(ROOT)):
                plist = plistlib.loads(fixture_config.render(template, LABEL, "/home/someone", ROOT).encode())
                self.assertEqual(plist["Label"], LABEL)
                # An absolute path into this repository (gap (f)); step 7
                # drops `--loopback` when the tailnet listener lands.
                self.assertEqual(plist["ProgramArguments"], [f"{ROOT}/local-sd-db/sd-db.sh", "serve", "--loopback"])
                self.assertTrue(plist["KeepAlive"])
                self.assertEqual(plist["ThrottleInterval"], 30)
                self.assertEqual(plist["StandardErrorPath"], f"/home/someone/Library/Logs/{LABEL}.err")


class Case(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.destroy)
        fixture_config.seal(self, self.fixture.stubs)
        self.root = pathlib.Path(self.fixture.tmp.name)
        self.config = fixture_config.copy_config(self.root)
        self.profiles = self.config / "machine-setup/profiles"

    def run_stage(self, stage, *flags, **extra):
        result = self.fixture.run("update", stage, *flags, **fixture_config.env(self.config), **extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout


class TheHub(Case):
    """Step 8 on the hub: zero drift once installed; each gap is one line."""

    def setUp(self):
        super().setUp()
        self.fixture.make_database()
        self.fixture.serve_status.write_text(ROUTE_PRESENT)
        self.agents = self.fixture.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        for label in manifest("personal.agent"):
            template = self.config / f"machine-setup/launchagents/{label}.plist"
            (self.agents / f"{label}.plist").write_text(
                fixture_config.render(template, label, self.fixture.home, ROOT))

    def hub_drift(self):
        return markers(self.run_stage("sd") + self.run_stage("agents"))

    def test_an_installed_hub_counts_zero_drift_and_a_removed_plist_counts_one(self):
        self.assertEqual(self.hub_drift(), [])
        self.assertIn(f"  ok      {LABEL} in personal.agent", self.run_stage("sd"))
        (self.agents / f"{LABEL}.plist").unlink()
        drift = self.hub_drift()
        self.assertEqual(len(drift), 1, drift)
        self.assertIn(f"MISSING {LABEL}", drift[0])

    def test_a_profile_without_the_serve_agent_is_one_missing(self):
        agent = self.profiles / "personal.agent"
        agent.write_text("".join(line + "\n" for line in agent.read_text().splitlines() if line != LABEL))
        drift = self.hub_drift()
        self.assertEqual(drift, [f"  MISSING {LABEL} in personal.agent — the hub serves no satellite; "
                                 "add the label, then: machine-setup.sh update agents --apply"])

    def test_a_config_folder_without_the_template_is_one_missing(self):
        (self.config / f"machine-setup/launchagents/{LABEL}.plist").unlink()
        drift = self.hub_drift()
        self.assertEqual(len(drift), 1, drift)
        self.assertIn(f"MISSING {self.config}/machine-setup/launchagents/{LABEL}.plist", drift[0])

    def test_a_hub_json_on_the_hub_is_extra_and_stays(self):
        stray = self.fixture.home / ".config/sd/hub.json"
        stray.parent.mkdir(parents=True)
        stray.write_text('{"hub": "hub.example.test", "port": 8769}\n')
        drift = markers(self.run_stage("sd", "--apply"))
        self.assertEqual(drift, [f"  EXTRA   {stray} — this machine is the sd hub, and that file makes it "
                                 "a satellite too; remove it"])
        self.assertTrue(stray.exists(), "the stage removed hub.json")


class Hub:
    """A loopback `sd-db.sh serve` over a fresh database, with a registry beside it."""

    def __init__(self, root, *, version=None):
        self.root = pathlib.Path(root)
        self.root.mkdir()
        self.database = self.root / "sd.db"
        initialise(self.database)
        (self.root / "providers.yaml").write_text(REGISTRY)
        self.token_file = self.root / "sd.db.serve.token"
        self.log = self.root / "serve.log"
        command = ["sh", str(SD_DB_SH), "serve"]
        if version is not None:
            # Another tag of the same build: the package says `version`.
            command = [sys.executable, "-c",
                       f"import sys, sd_db; sd_db.__version__ = {version!r}; "
                       "from sd_db import serve; sys.exit(serve.main(sys.argv[1:]))"]
        # The checkout's sd_db on both sides, whatever this interpreter has
        # installed: the satellite's stub python runs the checkout too.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "PYTHON": sys.executable,
               "PYTHONPATH": str(ROOT / "local-sd-db"), "SD_DB_LIBRARY": "checkout"}
        with open(self.log, "w") as stream:
            self.process = subprocess.Popen(
                [*command, "--loopback", "--port", "0", "--database", str(self.database)],
                stdout=subprocess.DEVNULL, stderr=stream, env=env, cwd=str(self.root))
        deadline = time.monotonic() + 30
        while True:
            found = re.search(r"on 127\.0\.0\.1:(\d+)", self.log.read_text())
            if found and self.token_file.exists():
                self.port = int(found.group(1))
                return
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.stop()
                raise AssertionError(f"serve did not start: {self.log.read_text()}")
            time.sleep(0.05)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=30)


class TheSatellite(Case):
    """Step 9: the `satellite` stage of a `work` profile that names the hub."""

    def setUp(self):
        super().setUp()
        (self.fixture.state / "profile").write_text("work\n")
        # A satellite holds no registry until the stage installs the hub's.
        self.fixture.registry.unlink()
        self.hub_json = self.fixture.home / ".config/sd/hub.json"

    def start_hub(self, **options):
        hub = Hub(self.root / "hub", **options)
        self.addCleanup(hub.stop)
        (self.profiles / "work.satellite").write_text(f"# The sd hub.\n127.0.0.1:{hub.port}\n")
        return hub

    def name_hub(self, hub):
        """hub.json as the stage writes it, plus the loopback token's file."""
        self.hub_json.parent.mkdir(parents=True, exist_ok=True)
        self.hub_json.write_text(json.dumps({"hub": "127.0.0.1", "port": hub.port,
                                             "token_file": str(hub.token_file)}))

    def satellite(self, *flags):
        return self.run_stage("satellite", *flags, SD_DB_PYTHON=self.fixture.python)

    def test_a_profile_without_a_hub_skips(self):
        out = self.satellite()
        self.assertIn("SKIP    no sd hub in this profile (work.satellite)", out)
        self.assertEqual(markers(out), [])

    def test_criterion_7_a_deleted_hub_json_counts_one_missing(self):
        hub = self.start_hub()
        self.name_hub(hub)
        self.satellite("--apply")
        self.assertEqual(self.fixture.registry.read_text(), REGISTRY)
        self.assertEqual(markers(self.satellite()), [])
        self.hub_json.unlink()
        drift = markers(self.satellite())
        self.assertEqual(drift, [f"  MISSING {self.hub_json} — this machine names no sd hub"])

    def test_apply_writes_hub_json_naming_the_profiles_hub(self):
        hub = self.start_hub()
        out = self.satellite("--apply")
        self.assertEqual(json.loads(self.hub_json.read_text()), {"hub": "127.0.0.1", "port": hub.port})
        # Without the loopback token the hub refuses; on the tailnet the peer
        # is the credential (step 7).
        self.assertIn(f"DIFFERS sd hub 127.0.0.1:{hub.port} refused this machine", out)

    def test_a_tag_mismatch_prints_differs_with_both_versions(self):
        hub = self.start_hub(version="0.0.9")
        self.name_hub(hub)
        import sd_db

        drift = markers(self.satellite("--apply"))
        self.assertEqual(drift, [f"  DIFFERS sd_db package version: satellite {sd_db.__version__}, hub 0.0.9 — "
                                 "upgrade the hub's sd_db, restart its `sd-db.sh serve`, then rerun"])
        self.assertFalse(self.fixture.registry.exists())

    def test_a_local_database_is_extra_and_hub_json_is_not_written(self):
        self.start_hub()
        self.fixture.make_database()
        drift = markers(self.satellite("--apply"))
        self.assertEqual(len(drift), 1, drift)
        self.assertTrue(drift[0].startswith(f"  EXTRA   database {self.fixture.db}"), drift)
        self.assertFalse(self.hub_json.exists())

    def test_a_profile_that_runs_the_hub_and_names_a_hub_differs(self):
        (self.fixture.state / "profile").write_text("personal\n")
        (self.profiles / "personal.satellite").write_text("hub.example.test\n")
        drift = markers(self.satellite())
        self.assertEqual(len(drift), 1, drift)
        self.assertIn("a machine is a hub or a satellite, not both", drift[0])

    def test_no_interpreter_is_missing(self):
        self.start_hub()
        out = self.run_stage("satellite", SD_DB_PYTHON=self.root / "no-python")
        self.assertEqual(markers(out), [f"  MISSING sd_db: no interpreter at {self.root / 'no-python'} — "
                                        "install the pack (python3 bin/sd_install.py --user), then rerun"])

    def test_status_runs_the_stage_beside_sd_and_before_agents(self):
        text = SCRIPT.read_text()
        stages = re.search(r'^STAGES="([^"]*)"', text, re.M).group(1).split()
        status = re.search(r"^  for st in ([^;]*); do$", text, re.M).group(1).split()
        for order in (stages, status):
            self.assertEqual(order.index("satellite"), order.index("sd") + 1, order)
            self.assertLess(order.index("satellite"), order.index("agents"), order)


if __name__ == "__main__":
    unittest.main()
