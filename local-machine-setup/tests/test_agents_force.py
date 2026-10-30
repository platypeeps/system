"""A launch agent plist nothing was recorded for still yields to --force.

The agents stage's no-record DIFFERS line tells the reader to review, then
run 'capture --apply' or --force. Until the dotfiles stage was fixed (#420)
the same line there offered --force and --force did nothing; this is the
same branch in the agents stage. Most runs are dry runs, so `run` prints the
cp and launchctl calls instead of making them; the --apply case puts a
`launchctl` stub first on PATH that records its arguments.
"""

import hashlib
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
SCRIPT = FOLDER / "machine-setup.sh"
PROFILE = "personal"


def first_agent():
    """The first agent whose plist the stage owns: the pack's installer writes sd-dashboard's (sd:6707)."""
    for line in (fixture_config.PROFILES / f"{PROFILE}.agent").read_text().splitlines():
        label = line.strip()
        if label.endswith(".sd-dashboard"):
            continue
        if label and not label.startswith("#") and (fixture_config.AGENTS / f"{label}.plist").is_file():
            return label
    raise AssertionError(f"no {PROFILE}.agent label has a plist in launchagents/")


class AgentCase(unittest.TestCase):
    """The profile's first agent, installed and then edited by hand."""

    def agent(self):
        return first_agent()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text(f"{PROFILE}\n")
        self.label = self.agent()
        plist = self.home / "Library/LaunchAgents" / f"{self.label}.plist"
        plist.parent.mkdir(parents=True)
        # Installed as the stage would render it, then edited by hand.
        rendered = fixture_config.render(fixture_config.AGENTS / f"{self.label}.plist",
                                         self.label, self.home, FOLDER.parent)
        self.rendered = rendered
        self.plist = plist
        plist.write_text(rendered + "<!-- edited -->\n")

    def run_agents(self, *flags, path=None, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": path or os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            **extra,
        }
        return subprocess.run([str(SCRIPT), "update", "agents", *flags], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name, timeout=120)


class AgentsForceTest(AgentCase):
    def run_stage(self, *flags, path=None):
        result = self.run_agents(*flags, path=path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.splitlines()

    def verdict(self, lines):
        return [line for line in lines if f" {self.label} " in line and not line.startswith("  [dry-run]")]

    def test_without_force_the_stage_only_reports(self):
        lines = self.run_stage()
        self.assertEqual(self.verdict(lines),
                         [f"  DIFFERS {self.label} — machine and repo disagree, nothing recorded says which moved; "
                          "review, then 'capture --apply' or --force"])
        self.assertFalse([line for line in lines if line.startswith("  [dry-run]") and self.label in line])

    def test_force_overwrites_a_plist_nothing_was_recorded_for(self):
        lines = self.run_stage("--force")
        self.assertEqual(self.verdict(lines),
                         [f"  DIFFERS {self.label} — nothing recorded; --force overwrites (backup kept)"])
        planned = [line for line in lines if line.startswith("  [dry-run]")]
        self.assertTrue(any(" cp " in line and self.label in line for line in planned), lines)
        self.assertTrue(any("launchctl bootstrap" in line for line in planned), lines)

    def test_force_with_apply_backs_up_installs_records_and_reloads(self):
        stubs = pathlib.Path(self.tmp.name) / "stubs"
        stubs.mkdir()
        calls = pathlib.Path(self.tmp.name) / "launchctl.log"
        stub = stubs / "launchctl"
        # print: launchd holds no job by that label, as after a bootout.
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n[ "$1" != print ] || exit 113\nexit 0\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        edited = self.plist.read_text()
        lines = self.run_stage("--apply", "--force",
                               path=f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        self.assertEqual(self.verdict(lines),
                         [f"  DIFFERS {self.label} — nothing recorded; --force overwrites (backup kept)"])
        self.assertEqual(self.plist.read_text(), self.rendered)
        backups = list(self.plist.parent.glob(f"{self.label}.plist.bak-*"))
        self.assertEqual([backup.read_text() for backup in backups], [edited])
        record = self.state / "installed" / f"agents_{self.label}.sha"
        self.assertEqual(record.read_text().strip(), hashlib.sha256(self.rendered.encode()).hexdigest())
        # The profile's other agents are missing here and get bootstrapped too.
        uid = os.getuid()
        self.assertEqual([call for call in calls.read_text().splitlines()
                          if call.split()[-1].split("/")[-1] in (self.label, f"{self.label}.plist")],
                         [f"bootout gui/{uid}/{self.label}", f"print gui/{uid}/{self.label}",
                          f"bootstrap gui/{uid} {self.plist}"])


# launchd after a bootout that returns before the job is gone (sd:6454):
# print still names $STUB_LABEL $STUB_PRINTS more times, and its bootstrap
# fails exit 5 until then. Every call is logged.
UNLOADING_STUB = r"""#!/bin/sh
echo "$*" >> "$STUB_LOG"
left=$(cat "$STUB_LOG.left" 2>/dev/null || echo 0)
case "$1 ${2##*/} ${3:-}" in
  "bootout $STUB_LABEL "*) echo "$STUB_PRINTS" > "$STUB_LOG.left" ;;
  "print $STUB_LABEL "*) [ "$left" -gt 0 ] || exit 113
                        echo $((left - 1)) > "$STUB_LOG.left"
                        echo "$2 = {" ;;
  "print "*) exit 113 ;;
  "bootstrap "*"/$STUB_LABEL.plist") [ "$left" -eq 0 ] || exit 5 ;;
esac
exit 0
"""


class BootoutWaitTest(AgentCase):
    """sd:6454: STALE sd-serve, kev and cswap-auto each failed `launchctl
    bootstrap` with exit 5 straight after their bootout; a bootstrap seconds
    later exited 0."""

    def replace(self, prints):
        """`update agents --apply --force`, whose bootout leaves the job
        printing PRINTS more times."""
        stubs = pathlib.Path(self.tmp.name) / "stubs"
        stubs.mkdir()
        stub = stubs / "launchctl"
        stub.write_text(UNLOADING_STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.calls = pathlib.Path(self.tmp.name) / "launchctl.log"
        return self.run_agents("--apply", "--force",
                               path=f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                               STUB_LOG=str(self.calls), STUB_LABEL=self.label,
                               STUB_PRINTS=str(prints))

    def mine(self):
        return [call for call in self.calls.read_text().splitlines()
                if call.split()[-1].split("/")[-1] in (self.label, f"{self.label}.plist")]

    def test_the_bootstrap_waits_until_launchd_lets_the_job_go(self):
        result = self.replace(prints=2)

        self.assertNotIn("FAILED", result.stdout, result.stdout + result.stderr)
        uid = os.getuid()
        self.assertEqual(self.mine(), [f"bootout gui/{uid}/{self.label}"]
                         + [f"print gui/{uid}/{self.label}"] * 3
                         + [f"bootstrap gui/{uid} {self.plist}"])
        self.assertEqual(self.plist.read_text(), self.rendered)

    def test_a_job_that_never_leaves_fails_and_keeps_its_plist(self):
        """The wait is bounded; past it the bootout is a failure, and the
        new plist is not put in place, so the next run retries."""
        edited = self.plist.read_text()
        result = self.replace(prints=1000)

        uid = os.getuid()
        self.assertIn(f"  FAILED  launchctl bootout gui/{uid}/{self.label} (still loaded after 10s)",
                      result.stdout, result.stdout + result.stderr)
        self.assertNotIn(f"bootstrap gui/{uid} {self.plist}", self.mine())
        self.assertEqual(self.plist.read_text(), edited)
        self.assertFalse(self.plist.with_name(self.plist.name + ".new").exists())


class PackOwnedDashboardTest(AgentCase):
    """sd:6707: the pack's `dashboard.sh install` writes the sd-dashboard
    plist (its `/bin/bash -c` start, sd:6444, its `--config` and env), so a
    plist that differs from the template is no drift, and --force must not
    put the template back over the pack's verified install."""

    def agent(self):
        return f"{fixture_config.LABEL_PREFIX}.sd-dashboard"

    def run_loaded(self, *flags):
        stubs = pathlib.Path(self.tmp.name) / "stubs"
        stubs.mkdir(exist_ok=True)
        stub = stubs / "launchctl"
        # print: launchd runs every job; anything else is recorded and passes.
        stub.write_text('#!/bin/sh\n[ "$1" != print ] || { echo "$2 = {"; exit 0; }\nexit 0\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        result = self.run_agents(*flags, path=f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.splitlines()

    def test_a_pack_written_plist_is_no_drift(self):
        for flags in ((), ("--force",)):
            lines = self.run_loaded(*flags)
            mine = [line for line in lines if f" {self.label}" in line]
            self.assertIn(f"  ok      {self.label} — the pack's dashboard installer owns this plist", mine, lines)
            self.assertFalse([line for line in mine if "DIFFERS" in line or " cp " in line], mine)
        self.assertTrue(self.plist.read_text().endswith("<!-- edited -->\n"))


class ForceHelp(unittest.TestCase):
    def test_help_names_the_stages_force_overwrites(self):
        # sd:3546: help promised --force for a .env, and the envs stage never
        # reads it; a live .env holds credentials the template omits.
        result = subprocess.run([str(SCRIPT), "help"], capture_output=True, text=True,
                                env={**os.environ, **fixture_config.env()})
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        self.assertIn("--force overwrites a DIFFERS dotfile or launch agent too, keeping a .bak- copy; "
                      "a .env is never overwritten", text)


if __name__ == "__main__":
    unittest.main()
