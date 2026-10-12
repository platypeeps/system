"""One failing remediation is reported and the run goes on (sd:3543).

do_stages runs each stage bare under `set -e`, and the stage loops are
pipelines. A bare `run` whose command failed ended the loop, the stage and the
run: one dead formula stopped `setup --apply` before every later formula,
cask and stage. `run` now prints a FAILED line, the run goes on, and the run
ends with a summary of what failed and exit 1, so a nightly caller still sees
it. Stubs on PATH stand in for brew and launchctl; nothing real runs.
"""

import hashlib
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent

# Installs nothing; `install bad-formula` fails, every call is logged.
BREW_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$BREW_LOG"
case "$*" in
  "install bad-formula") echo "Error: bad-formula: download failed" >&2; exit 1 ;;
esac
exit 0
"""

# Nothing is loaded; bootout fails as it does for a label launchd does not
# hold, and bootstrap fails for the agent named in $BOOTSTRAP_FAILS.
LAUNCHCTL_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  print|bootout) exit 1 ;;
  bootstrap) case "$3" in *"/$BOOTSTRAP_FAILS.plist") exit 5 ;; esac ;;
esac
exit 0
"""


def write_exec(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class RemediationFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.config = fixture_config.copy_config(self.base)
        self.profiles = self.config / "machine-setup/profiles"
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        self.brew_log = self.base / "brew.log"
        self.launchctl_log = self.base / "launchctl.log"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "launchctl", LAUNCHCTL_STUB)
        fixture_config.seal(self, self.stubs)

    def run_verb(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "BREW_LOG": str(self.brew_log),
            "LAUNCHCTL_LOG": str(self.launchctl_log),
            **extra,
        }
        return subprocess.run([str(FOLDER / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def log(self, path):
        return path.read_text().splitlines() if path.exists() else []

    def test_a_failed_formula_does_not_stop_the_formulae_and_casks_after_it(self):
        (self.profiles / "personal.brew").write_text("bad-formula\ngood-formula\n")
        (self.profiles / "personal.cask").write_text("good-cask\n")
        result = self.run_verb("update", "brew", "--apply")
        out = result.stdout
        self.assertIn("install good-formula", self.log(self.brew_log), out + result.stderr)
        self.assertIn("install --cask --adopt good-cask", self.log(self.brew_log))
        self.assertIn("  FAILED  brew install bad-formula (exit 1)\n", out)
        # The summary prints after the stage loop, so the loop went on.
        self.assertIn("failed — 1 remediation(s) did not finish; fix them, then re-run:\n"
                      "    brew install bad-formula\n", out)
        self.assertEqual(result.returncode, 1, out + result.stderr)

    def test_a_run_with_no_failure_prints_no_summary_and_exits_0(self):
        (self.profiles / "personal.brew").write_text("good-formula\n")
        result = self.run_verb("update", "brew", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAILED", result.stdout)
        self.assertNotIn("did not finish", result.stdout)

    def test_a_refused_bootstrap_is_reported_and_the_next_agent_still_loads(self):
        labels = [line.strip() for line in (self.profiles / "personal.agent").read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        self.assertGreaterEqual(len(labels), 2)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        result = self.run_verb("update", "agents", "--apply", BOOTSTRAP_FAILS=labels[0])
        out = result.stdout
        bootstraps = [line for line in self.log(self.launchctl_log) if line.startswith("bootstrap ")]
        self.assertTrue(any(line.endswith(f"/{labels[1]}.plist") for line in bootstraps), out + result.stderr)
        self.assertIn("  FAILED  launchctl bootstrap gui/", out)
        self.assertIn(f"/{labels[0]}.plist (exit 5)\n", out)
        self.assertEqual(result.returncode, 1, out + result.stderr)

    def test_a_bootout_of_a_label_launchd_does_not_hold_is_not_a_failure(self):
        # STALE: the installed plist is the recorded one, the repo moved. The
        # stage boots the old job out before it bootstraps the new file, and
        # bootout fails when launchd holds nothing under that label.
        label, _ = self.stale_agent()
        result = self.run_verb("update", "agents", "--apply")
        self.assertIn(f"  STALE   {label}", result.stdout, result.stdout + result.stderr)
        self.assertIn("bootout gui/", "\n".join(self.log(self.launchctl_log)))
        self.assertNotIn("FAILED", result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def stale_agent(self):
        """An installed plist this run sees as STALE: recorded, and the repo moved."""
        label = "local.system-tools.sd-serve"
        (self.profiles / "personal.agent").write_text(f"{label}\n")
        plist = self.home / "Library/LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist>older</plist>\n")
        installed = self.state / "installed"
        installed.mkdir()
        digest = hashlib.sha256(b"<plist>older</plist>\n").hexdigest()
        (installed / f"agents_{label}.sha").write_text(digest + "\n")
        return label, plist

    def test_a_failed_backup_skips_the_overwrite_it_guards(self):
        # Under set -e a failed backup ended the run before the overwrite.
        # run now goes on, so the backup's failure must still stop the copy.
        label, plist = self.stale_agent()
        write_exec(self.stubs / "cp", '#!/bin/sh\ncase "$*" in *.bak-*) exit 1 ;; esac\nexec /bin/cp "$@"\n')
        result = self.run_verb("update", "agents", "--apply")
        out = result.stdout
        self.assertIn(f"  FAILED  backup_file {plist} (exit 1)\n", out, out + result.stderr)
        self.assertEqual(plist.read_text(), "<plist>older</plist>\n")
        self.assertFalse([line for line in self.log(self.launchctl_log) if line.startswith("bootstrap ")])
        self.assertEqual(result.returncode, 1)

    def test_a_failed_env_append_is_not_recorded_so_the_next_run_retries(self):
        template = self.config / "machine-setup/envs/personal/local-notify.env"
        template.parent.mkdir(parents=True)
        template.write_text("A=1\nB=2\n")
        live = self.config / "notify/.env"
        live.parent.mkdir(parents=True)
        live.write_text("A=1\n")
        record = self.state / "installed/envs_local-notify.sha"
        record.parent.mkdir(parents=True)
        older = hashlib.sha256(b"A=1\n").hexdigest()
        record.write_text(older + "\n")
        live.chmod(0o400)
        first = self.run_verb("update", "envs", "--apply")
        self.assertIn(f"  FAILED  append B to {live} (exit ", first.stdout, first.stdout + first.stderr)
        self.assertEqual(first.returncode, 1)
        self.assertEqual(record.read_text().strip(), older)
        live.chmod(0o600)
        # The first run's backup kept the 0400 mode and, inside one second,
        # its name: the retry would fail to overwrite it.
        for backup in live.parent.glob(".env.bak-*"):
            backup.unlink()
        second = self.run_verb("update", "envs", "--apply")
        self.assertIn("appended from the template", second.stdout, second.stdout + second.stderr)
        self.assertEqual(live.read_text(), "A=1\nB=2\n")
        self.assertEqual(record.read_text().strip(), hashlib.sha256(b"A=1\nB=2\n").hexdigest())
        self.assertEqual(second.returncode, 0)


if __name__ == "__main__":
    unittest.main()
