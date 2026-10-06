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
from tests.test_backbone import DRIFT_WORDS, FOLDER, ROOT, ROUTE_PRESENT, SCRIPT, Fixture, manifest, write_stub

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


# The hub's launchd jobs as the script lists them, so no test keeps a copy.
HUB_ONLY = [f"{fixture_config.LABEL_PREFIX}.{suffix}" for suffix in
            re.search(r'^SD_HUB_ONLY_AGENTS="([^"]*)"', SCRIPT.read_text(), re.M).group(1).split()]


class TheServeAgent(unittest.TestCase):
    """The template and the profile line (step 8, gap (f))."""

    def test_personal_agent_names_the_serve_agent(self):
        self.assertIn(LABEL, manifest("personal.agent"))

    def test_the_template_execs_this_checkouts_serve_on_the_tailnet(self):
        for template in (fixture_config.AGENTS / f"{LABEL}.plist",
                         FOLDER / f"examples/launchagents/{LABEL}.plist"):
            with self.subTest(template=template.relative_to(ROOT)):
                plist = plistlib.loads(fixture_config.render(template, LABEL, "/home/someone", ROOT).encode())
                self.assertEqual(plist["Label"], LABEL)
                # An absolute path into this repository (gap (f)), and no
                # `--loopback`: the tailnet listener of step 7.
                self.assertEqual(plist["ProgramArguments"], [f"{ROOT}/local-sd-db/sd-db.sh", "serve"])
                # launchd's own PATH holds no `tailscale`, which the listener runs.
                self.assertIn("/opt/homebrew/bin", plist["EnvironmentVariables"]["PATH"].split(":"))
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

    def run_stage(self, stage, *flags, fixture=None, **extra):
        fixture = fixture or self.fixture
        result = fixture.run("update", stage, *flags, **fixture_config.env(self.config), **extra)
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

    def test_criterion_7_a_hub_json_on_the_hub_counts_one_extra_across_the_sd_stages(self):
        stray = self.fixture.home / ".config/sd/hub.json"
        stray.parent.mkdir(parents=True)
        stray.write_text('{"hub": "hub.example.test", "port": 8769}\n')
        out = self.run_stage("sd") + self.run_stage("satellite") + self.run_stage("agents")
        drift = markers(out)
        self.assertEqual(len(drift), 1, drift)
        self.assertIn(f"EXTRA   {stray}", drift[0])


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
        self.agents = self.fixture.home / "Library/LaunchAgents"
        self.launchctl_log = self.stub_launchd(self.fixture)
        # A jev on this machine's PATH reads the fixture home: shadow is set,
        # so the cases below see no jev drift whether jev is installed or not.
        self.shadow = self.fixture.home / ".config/jev/shadow"
        self.shadow.parent.mkdir(parents=True)
        self.shadow.write_text("on\n")

    @staticmethod
    def stub_launchd(fixture):
        """launchd runs only the labels in STUB_LOADED. Every call is logged,
        so a test can see the stage never boots anything out."""
        log = pathlib.Path(fixture.tmp.name) / "launchctl.log"
        write_stub(fixture.stubs, "launchctl",
                   f'echo "$*" >> "{log}"\n'
                   'case "$1" in print) case " $STUB_LOADED " in *" ${2##*/} "*) exit 0 ;; esac; exit 113 ;; esac\n'
                   'exit 0\n')
        return log

    def start_hub(self, **options):
        hub = Hub(self.root / "hub", **options)
        self.addCleanup(hub.stop)
        (self.profiles / "work.satellite").write_text(f"# The sd hub.\n127.0.0.1:{hub.port}\n")
        return hub

    def name_hub(self, hub, fixture=None):
        """hub.json as the stage writes it, plus the loopback token's file."""
        hub_json = (fixture or self.fixture).home / ".config/sd/hub.json"
        hub_json.parent.mkdir(parents=True, exist_ok=True)
        hub_json.write_text(json.dumps({"hub": "127.0.0.1", "port": hub.port,
                                        "token_file": str(hub.token_file)}))

    def satellite(self, *flags, loaded=(), fixture=None):
        fixture = fixture or self.fixture
        return self.run_stage("satellite", *flags, fixture=fixture, SD_DB_PYTHON=fixture.python,
                              STUB_LOADED=" ".join(loaded))

    def test_two_satellites_with_their_own_homes_pass_against_one_hub(self):
        # One machine per profile: work and terra, each with its own home
        # and its own hub.json, against the one hub.
        hub = self.start_hub()
        (self.profiles / "terra.satellite").write_text(f"127.0.0.1:{hub.port}\n")
        terra = Fixture()
        self.addCleanup(terra.destroy)
        fixture_config.seal(self, terra.stubs)
        (terra.state / "profile").write_text("terra\n")
        terra.registry.unlink()
        self.stub_launchd(terra)
        self.assertNotEqual(self.fixture.home, terra.home)
        for fixture in (self.fixture, terra):
            with self.subTest(home=fixture.home):
                self.name_hub(hub, fixture)
                self.satellite("--apply", fixture=fixture)
                self.assertEqual(fixture.registry.read_text(), REGISTRY)
                self.assertEqual(markers(self.satellite(fixture=fixture)), [])
                self.assertFalse(fixture.db.exists())
        # The first satellite still passes after the second joined.
        self.assertEqual(markers(self.satellite()), [])

    def launchctl_verbs(self):
        text = self.launchctl_log.read_text() if self.launchctl_log.exists() else ""
        return {line.split()[0] for line in text.splitlines()}

    def test_a_hub_only_plist_on_a_satellite_is_extra_and_stays(self):
        self.name_hub_by_hand()
        self.agents.mkdir(parents=True)
        for label in HUB_ONLY:
            (self.agents / f"{label}.plist").write_text("<plist/>\n")
        drift = markers(self.satellite("--apply"))
        self.assertEqual(drift, [f"  EXTRA   {self.agents / label}.plist — hub only, and this machine is a "
                                 "satellite; remove it by hand" for label in HUB_ONLY])
        self.assertEqual(sorted(p.name for p in self.agents.iterdir()), sorted(f"{l}.plist" for l in HUB_ONLY))
        self.assertLessEqual(self.launchctl_verbs(), {"print"})

    def test_a_loaded_hub_agent_with_no_plist_is_extra(self):
        (self.profiles / "work.satellite").write_text("hub.example.test\n")
        runner = f"{fixture_config.LABEL_PREFIX}.sd-runner"
        out = self.satellite("--apply", loaded=[runner])
        extra = [line for line in markers(out) if line.startswith("  EXTRA")]
        self.assertEqual(extra, [f"  EXTRA   {runner} loaded with no plist — hub only, and this machine is "
                                 "a satellite; boot it out by hand"])
        self.assertLessEqual(self.launchctl_verbs(), {"print"})

    def test_a_satellite_with_no_hub_agent_counts_no_extra(self):
        self.name_hub_by_hand()
        self.assertEqual(markers(self.satellite()), [])

    def test_the_agents_stage_skips_hub_only_agents_while_hub_json_exists(self):
        # A guard: no satellite profile lists a hub agent today. One that
        # adds every hub LaunchAgent later installs none of them.
        self.name_hub_by_hand()
        self.agents.mkdir(parents=True)
        listed = [label for label in HUB_ONLY if ".cron." not in label]
        for profile in ("work", "terra"):
            with self.subTest(profile=profile):
                (self.fixture.state / "profile").write_text(f"{profile}\n")
                (self.profiles / f"{profile}.agent").write_text("".join(f"{label}\n" for label in listed))
                out = self.run_stage("agents", "--apply")
                self.assertEqual(markers(out), [])
                for label in listed:
                    self.assertIn(f"  SKIP    {label} — hub only, and {self.hub_json} makes this machine a "
                                  "satellite", out)
                self.assertEqual(list(self.agents.iterdir()), [])
                self.assertNotIn("bootstrap", self.launchctl_verbs())

    def name_hub_by_hand(self):
        """A hub.json and no `.satellite`: the stage has no hub to ask."""
        self.hub_json.parent.mkdir(parents=True, exist_ok=True)
        self.hub_json.write_text('{"hub": "hub.example.test", "port": 8769}\n')

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

    def test_a_personal_satellite_on_the_hub_is_refused_by_name(self):
        (self.fixture.state / "profile").write_text("personal\n")
        (self.profiles / "personal.satellite").write_text("hub.example.test\n")
        drift = markers(self.satellite("--apply"))
        self.assertEqual(drift, ["  DIFFERS personal.satellite names the hub hub.example.test, and personal.agent "
                                 "runs the hub's agents; the hub cannot be its own satellite — remove "
                                 "personal.satellite"])
        self.assertFalse(self.hub_json.exists())

    def stub_jev(self):
        """`jev shadow on` as jev.py does it, each call logged."""
        log = self.root / "jev.log"
        write_stub(self.fixture.stubs, "jev",
                   f'echo "$*" >> "{log}"\n'
                   '[ "$1 $2" = "shadow on" ] && mkdir -p "$HOME/.config/jev" && echo on > "$HOME/.config/jev/shadow"\n'
                   'exit 0\n')
        return log

    def jev_satellite(self, *flags, **extra):
        (self.profiles / "work.satellite").write_text("hub.example.test\n")
        python = write_stub(self.root, "stub-python", 'echo "  ok      sd_db satellite"\n')
        return self.run_stage("satellite", *flags, SD_DB_PYTHON=python, **extra)

    def test_a_satellite_without_a_shadow_file_gets_one_and_no_arms(self):
        # No shadow file means live, and a satellite's live rows reached the
        # hub's ledger (sd:2838). One MISSING; --apply runs `jev shadow on`.
        log = self.stub_jev()
        self.shadow.unlink()
        out = self.jev_satellite()
        self.assertEqual(markers(out), [f"  MISSING {self.shadow} — Jev would answer live on this satellite"])
        self.assertIn("[dry-run] jev shadow on", out)
        self.assertFalse(self.shadow.exists())
        self.assertEqual(markers(self.jev_satellite("--apply")), [f"  MISSING {self.shadow} — Jev would answer live on this satellite"])
        self.assertEqual(self.shadow.read_text(), "on\n")
        # Idempotent: the second run finds it and calls nothing.
        out = self.jev_satellite("--apply")
        self.assertEqual(markers(out), [])
        self.assertIn(f"  ok      jev shadow file {self.shadow} (on)", out)
        self.assertEqual(log.read_text().splitlines(), ["shadow on"])

    def test_a_written_shadow_off_is_the_operators_and_stays(self):
        log = self.stub_jev()
        self.shadow.write_text("off\n")
        out = self.jev_satellite("--apply")
        self.assertEqual(markers(out), [])
        self.assertEqual(self.shadow.read_text(), "off\n")
        self.assertFalse(log.exists())

    def test_a_satellite_without_jev_skips_the_shadow_silently(self):
        self.shadow.unlink()
        out = self.jev_satellite("--apply", PATH=f"{self.fixture.stubs}:/usr/bin:/bin")
        self.assertNotIn("jev", out)
        self.assertEqual(markers(out), [])
        self.assertFalse(self.shadow.exists())

    def test_the_hub_never_writes_a_shadow_file(self):
        log = self.stub_jev()
        self.shadow.unlink()
        (self.fixture.state / "profile").write_text("personal\n")
        for satellite in (None, "hub.example.test\n"):
            with self.subTest(satellite=satellite):
                if satellite:
                    (self.profiles / "personal.satellite").write_text(satellite)
                self.run_stage("satellite", "--apply")
                self.assertFalse(self.shadow.exists())
                self.assertFalse(log.exists())

    def test_the_stage_names_its_own_checkout_as_the_source_to_install_from(self):
        # sd:2802: `--apply` installs the hub's build from origin/main of a
        # system checkout; the stage names its own, and an exported value wins.
        (self.profiles / "work.satellite").write_text("hub.example.test\n")
        write_stub(self.root, "stub-python", 'echo "  ok      source=$SD_DB_SOURCE_CHECKOUT args=$*"\n')
        python = self.root / "stub-python"
        out = self.run_stage("satellite", "--apply", SD_DB_PYTHON=python)
        self.assertIn(f"  ok      source={ROOT} args=-I -m sd_db.satellite --hub hub.example.test --apply", out)
        chosen = self.root / "elsewhere"
        out = self.run_stage("satellite", SD_DB_PYTHON=python, SD_DB_SOURCE_CHECKOUT=chosen)
        self.assertIn(f"  ok      source={chosen} args=", out)

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
