"""`run` hands its findings to the run's report, and mails nothing (sd:3454).

The script runs from a copy whose sibling `local-notify/notify.sh` is a stub
that records any call, so no run of this suite can reach a real channel.
`softwareupdate`, `mas`, `docker` and `hostname` are stubs on PATH; the one
finding comes from a conf line naming a certificate that does not exist.
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"


def _stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class Harness(unittest.TestCase):
    """A copy of the script with stubbed tools; no tests of its own."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="maintenance-report."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "local-maintenance").mkdir()
        shutil.copy(FOLDER / "maintenance.sh", self.tmp / "local-maintenance")
        shutil.copy(FOLDER / "prune_move.py", self.tmp / "local-maintenance")
        shutil.copytree(LIB, self.tmp / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.sent = self.tmp / "notify-calls"
        _stub(self.tmp / "local-notify" / "notify.sh", f'printf "%s\\n" "$@" >> "{self.sent}"\n')
        self.bin = self.tmp / "bin"
        _stub(self.bin / "softwareupdate", 'echo "No new software available."\n')
        _stub(self.bin / "mas", "exit 0\n")
        # sudo records each call and runs nothing, so no test reaches the real one.
        self.sudo_calls = self.tmp / "sudo-calls"
        _stub(self.bin / "sudo", f'echo "$SUDO_ASKPASS $*" >> "{self.sudo_calls}"\n')
        _stub(self.bin / "docker", "exit 1\n")
        _stub(self.bin / "hostname", 'echo "fixturehost"\n')
        # FAKE_DAY stands for the run's date, which the reason must not carry.
        # The weekday is FAKE_WEEKDAY, Monday unless a test sets it: the real
        # one made the Sunday-only docker step run, and fail a clean run, on Sundays.
        _stub(self.bin / "date", '''[ -n "$FAKE_DAY" ] && [ "$1" = "+%Y-%m-%d" ] && { echo "$FAKE_DAY"; exit 0; }
[ "$1" = "+%u" ] && { echo "${FAKE_WEEKDAY:-1}"; exit 0; }
exec /bin/date "$@"
''')
        # df reports DF_USED percent used, 50 unless a test sets it.
        _stub(self.bin / "df", 'echo "Filesystem 512-blocks Used Available Capacity Mounted on"\n'
              'echo "/dev/disk1 100 ${DF_USED:-50} 0 ${DF_USED:-50}% /"\n')
        self.config = self.tmp / "config"
        (self.config / "maintenance").mkdir(parents=True)
        (self.tmp / "home").mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "SYSTEM_TOOLS_CONFIG": str(self.config), "MAINTENANCE_PROFILE": "none"}

    def maintenance(self, conf, verb="run", **extra):
        (self.config / "maintenance" / "maintenance.conf").write_text(conf)
        return subprocess.run(["sh", str(self.tmp / "local-maintenance" / "maintenance.sh"), verb],
                              capture_output=True, text=True, env={**self.env, **extra}, timeout=120)

    def attention(self, run):
        return [line for line in run.stdout.splitlines() if line.startswith("SD_REPORT_ATTENTION: ")]


class ReportTest(Harness):
    def test_a_finding_needs_attention_and_mails_nothing(self):
        run = self.maintenance(f"cert|{self.tmp}/missing.pem|30\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(f"- cert: {self.tmp}/missing.pem missing", run.stdout)
        [line] = self.attention(run)
        self.assertEqual(line, f"SD_REPORT_ATTENTION: maintenance on fixturehost: cert: {self.tmp}/missing.pem missing")
        self.assertFalse(self.sent.exists(), self.sent.read_text() if self.sent.exists() else "")

    def test_the_reason_follows_the_findings_not_the_day(self):
        """sd:4182: the ingest folds a run into the open report with its reason.
        The same findings on two nights give one reason, so one open report;
        a changed finding gives another (sd:3537: a checksum with digits
        erased made worker1 and worker2 one report)."""
        first = self.attention(self.maintenance(f"cert|{self.tmp}/worker1.pem|30\n", FAKE_DAY="2026-10-12"))
        again = self.attention(self.maintenance(f"cert|{self.tmp}/worker1.pem|30\n", FAKE_DAY="2026-10-13"))
        other = self.attention(self.maintenance(f"cert|{self.tmp}/worker2.pem|30\n", FAKE_DAY="2026-10-13"))
        self.assertEqual(len(first), 1, first)
        self.assertEqual(again, first)
        self.assertNotEqual(other, first)
        # sd:4664: one line per finding, so a night with fewer findings prints a subset (pack sd:4658 folds it).
        both = self.attention(self.maintenance(f"cert|{self.tmp}/worker1.pem|30\ncert|{self.tmp}/worker2.pem|30\n"))
        self.assertEqual(both, first + other)

    def test_a_moving_measure_keeps_the_reason(self):
        """sd:4182: low disk at 5% free one night and 4% the next is the
        same finding, so the same reason; another disk is another finding.
        The report itself still prints each night's measure."""
        five = self.maintenance("disk|/|10\n", DF_USED="95")
        four = self.maintenance("disk|/|10\n", DF_USED="96")
        other = self.maintenance("disk|/Volumes/Other|10\n", DF_USED="96")
        self.assertIn("- disk: only 5% free on / (threshold 10%)", five.stdout)
        self.assertIn("- disk: only 4% free on / (threshold 10%)", four.stdout)
        self.assertEqual(self.attention(four), self.attention(five))
        self.assertEqual(len(self.attention(five)), 1, five.stdout)
        self.assertNotEqual(self.attention(other), self.attention(five))

    def test_each_pending_update_is_its_own_line_under_its_finding(self):
        """sd:4664: one line per pending update, so one update installed leaves a subset (pack sd:4658)."""
        _stub(self.bin / "mas", 'printf "%s\\n" "1 Example App (1.0 -> 1.1)" "2 Other App (2.0 -> 2.1)"\n')
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(self.attention(run), [
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store updates pending:",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store updates pending: 1 Example App (1.0 -> 1.1)",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store updates pending: 2 Other App (2.0 -> 2.1)"])

    def test_a_clean_run_needs_nothing(self):
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks clean", run.stdout)
        self.assertEqual(self.attention(run), [])

    def test_the_docker_prune_runs_on_sundays_only(self):
        run = self.maintenance("# nothing to check\n", FAKE_WEEKDAY="7")
        self.assertIn("docker prune skipped", run.stdout)
        self.assertNotIn("docker prune", self.maintenance("# nothing to check\n", FAKE_WEEKDAY="6").stdout)

    def test_a_mount_counts_only_at_its_own_mountpoint(self):
        """sd:3718: a mountpoint ends at " (" or " type ", not at a space, so
        "/Volumes/Offsite Disk" does not mount /Volumes/Offsite."""
        _stub(self.bin / "mount", 'echo "/dev/disk5s1 on /Volumes/Offsite Disk (apfs, local, journaled)"\n'
              'echo "server:/export on /Volumes/Share type nfs (rw)"\n')
        run = self.maintenance("mount|/Volumes/Offsite\nmount|/Volumes/Offsite Disk\nmount|/Volumes/Share\n"
                               "mount|/Volumes/Off\n", "check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        findings = [line for line in run.stdout.splitlines() if line.startswith("- mount: ")]
        self.assertEqual(findings, ["- mount: /Volumes/Offsite is not mounted",
                                    "- mount: /Volumes/Off is not mounted"])

    def test_each_update_check_runs_bounded_and_names_itself(self):
        """sd:3536: softwareupdate and mas run under st_step; the step line
        reaches the job log (stderr), not the report."""
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertRegex(run.stderr, r"\[step [\d:]+\] softwareupdate -l \(bound 300s\)")
        self.assertRegex(run.stderr, r"\[step [\d:]+\] mas outdated \(bound 120s\)")
        self.assertNotIn("[step ", run.stdout)

    def test_a_failed_app_store_check_is_a_finding(self):
        """A check that failed found nothing, which is not 'nothing pending'."""
        _stub(self.bin / "mas", 'echo "mas: cannot reach the App Store" >&2; exit 1\n')
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("- App Store update check failed (mas outdated exit 1)", run.stdout)

    def test_a_failed_softwareupdate_names_its_own_last_line(self):
        _stub(self.bin / "softwareupdate", 'echo "Cannot reach the update server" >&2; exit 1\n')
        run = self.maintenance("# nothing to check\n")
        self.assertIn("- softwareupdate check failed: Cannot reach the update server", run.stdout)

    def test_check_prints_findings_without_the_line(self):
        run = self.maintenance(f"cert|{self.tmp}/missing.pem|30\n", "check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("missing.pem missing", run.stdout)
        self.assertEqual(self.attention(run), [])


#: softwareupdate -l lists one update that needs no restart and one that does.
SU_LIST = (
    "Software Update found the following new or updated software:",
    "* Label: Command Line Tools for Xcode 27.2-27.2",
    "\tTitle: Command Line Tools for Xcode 27.2, Version: 27.2, Size: 521120KiB, Recommended: YES, ",
    "* Label: macOS Example 27.3-27D50",
    "\tTitle: macOS Example 27.3, Version: 27.3, Size: 9000000KiB, Recommended: YES, Action: restart, ",
)


class InstallTest(Harness):
    """sd:6927: `run` installs pending macOS updates that need no restart and
    upgrades App Store apps, through sudo -A and machine-setup's askpass;
    `check` installs nothing, and what stays pending stays a finding."""

    def setUp(self):
        super().setUp()
        lines = " ".join(f"'{line}'" for line in SU_LIST)
        _stub(self.bin / "softwareupdate", f'[ "$1" = -l ] && {{ printf "%s\\n" {lines}; exit 0; }}\nexit 0\n')
        # mas lists Xcode until `mas upgrade` ran.
        done = self.tmp / "mas-upgraded"
        _stub(self.bin / "mas", f'case "$1" in\n'
              f'  outdated) [ -f "{done}" ] || echo "497799835  Xcode  (27.1 -> 27.2)" ;;\n'
              f'  upgrade) : > "{done}" ;;\nesac\n')
        _stub(self.bin / "sudo", f'echo "$SUDO_ASKPASS $*" >> "{self.sudo_calls}"\n'
              '[ -n "$SUDO_REFUSE" ] && { echo "sudo: no password was provided" >&2; exit 1; }\n'
              '[ "$1" = -A ] && shift\nexec "$@"\n')

    def calls(self):
        return self.sudo_calls.read_text().splitlines() if self.sudo_calls.exists() else []

    def test_run_installs_what_needs_no_restart(self):
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        askpass = str(self.tmp / "local-maintenance" / ".." / "local-machine-setup" / "sudo-askpass.sh")
        self.assertEqual(self.calls(), [
            f"{askpass} -A softwareupdate -i Command Line Tools for Xcode 27.2-27.2",
            f"{askpass} -A mas upgrade 497799835"])
        self.assertIn("- installed macOS update Command Line Tools for Xcode 27.2-27.2", run.stdout)
        self.assertIn("- upgraded App Store app 497799835  Xcode  (27.1 -> 27.2)", run.stdout)
        self.assertEqual(self.attention(run), [
            "SD_REPORT_ATTENTION: maintenance on fixturehost: macOS updates pending:",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: macOS updates pending: macOS Example 27.3-27D50"])

    def test_each_install_runs_bounded(self):
        run = self.maintenance("# nothing to check\n")
        self.assertRegex(run.stderr, r"\[step [\d:]+\] sudo -A softwareupdate -i Command Line Tools"
                                     r" for Xcode 27.2-27.2 \(bound 1800s\)")
        self.assertRegex(run.stderr, r"\[step [\d:]+\] sudo -A mas upgrade 497799835 \(bound 600s\)")

    def test_check_installs_nothing(self):
        run = self.maintenance("# nothing to check\n", "check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIn("    Command Line Tools for Xcode 27.2-27.2", run.stdout)
        self.assertIn("    497799835  Xcode  (27.1 -> 27.2)", run.stdout)

    def test_a_refused_install_stays_pending_with_its_reason(self):
        run = self.maintenance("# nothing to check\n", SUDO_REFUSE="1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        # The report names sudo's reason; the key leaves it out, so the
        # reason of the report does not move with the message (sd:4182).
        self.assertIn("- macOS update install failed: Command Line Tools for Xcode 27.2-27.2:"
                      " sudo: no password was provided", run.stdout)
        self.assertIn("- App Store upgrade failed: Xcode: sudo: no password was provided", run.stdout)
        self.assertEqual(self.attention(run), [
            "SD_REPORT_ATTENTION: maintenance on fixturehost: macOS update install failed:"
            " Command Line Tools for Xcode 27.2-27.2",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: macOS updates pending:",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: macOS updates pending:"
            " Command Line Tools for Xcode 27.2-27.2",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: macOS updates pending: macOS Example 27.3-27D50",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store upgrade failed: Xcode",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store updates pending:",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store updates pending:"
            " 497799835  Xcode  (27.1 -> 27.2)"])


#: mas outdated as the hub listed it on 2026-11-02: mas pads its columns to the
#: longest name, so PeakHour's line changes once SimpleMind Pro is gone.
MAS_BOTH = ("1560576252  PeakHour        (6.2.1  -> 6.2.2)",
            " 434808346  SimpleMind Pro  (2.10.2 -> 2.10.3)")
MAS_PEAKHOUR = ("1560576252  PeakHour  (6.2.1 -> 6.2.2)",)


class AppStoreUpgradeTest(Harness):
    """sd:7188: `run` upgrades one App Store app at a time, by id, under a
    bound of its own, and reports an app as upgraded only when a check
    after the upgrades no longer lists its id."""

    def setUp(self):
        super().setUp()
        state = self.tmp / "mas-state"
        state.mkdir()
        both = " ".join(f"'{line}'" for line in MAS_BOTH)
        peakhour = " ".join(f"'{line}'" for line in MAS_PEAKHOUR)
        # PeakHour never upgrades. With PEAKHOUR_HANGS its download hangs, and
        # an upgrade of all apps hangs with it, as storedownloadd did.
        # OUTDATED_FAILS fails each check after the first upgrade.
        _stub(self.bin / "mas", f'''S="{state}"
case "$1" in
  outdated)
    [ -n "$OUTDATED_FAILS" ] && [ -f "$S/upgrade" ] && {{ echo "mas: no network" >&2; exit 1; }}
    if [ -f "$S/simplemind" ]; then printf "%s\\n" {peakhour}; else printf "%s\\n" {both}; fi ;;
  upgrade)
    : > "$S/upgrade"
    case "$2" in
      1560576252) [ -n "$PEAKHOUR_HANGS" ] && sleep 20 ;;
      434808346) : > "$S/simplemind" ;;
      "") [ -n "$PEAKHOUR_HANGS" ] && {{ sleep 20; exit 1; }}; : > "$S/simplemind" ;;
    esac ;;
esac
exit 0
''')
        _stub(self.bin / "sudo", f'echo "$*" >> "{self.sudo_calls}"\n[ "$1" = -A ] && shift\nexec "$@"\n')

    def upgraded(self, run):
        return [line for line in run.stdout.splitlines() if line.startswith("- upgraded App Store app ")]

    def test_a_hung_app_does_not_hold_the_next(self):
        run = self.maintenance("# nothing to check\n", PEAKHOUR_HANGS="1", MAINTENANCE_MAS_BOUND="2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.sudo_calls.read_text().splitlines(),
                         ["-A mas upgrade 1560576252", "-A mas upgrade 434808346"])
        self.assertIn("- App Store upgrade failed: PeakHour: timed out after 2s:"
                      " sudo -A mas upgrade 1560576252", run.stdout)
        self.assertEqual(self.upgraded(run),
                         ["- upgraded App Store app  434808346  SimpleMind Pro  (2.10.2 -> 2.10.3)"])
        self.assertEqual(self.attention(run), [
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store upgrade failed: PeakHour",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store updates pending:",
            "SD_REPORT_ATTENTION: maintenance on fixturehost: App Store updates pending:"
            " 1560576252  PeakHour  (6.2.1 -> 6.2.2)"])

    def test_an_app_still_listed_is_not_upgraded_when_its_padding_moves(self):
        """The 04:25 run on 2026-11-02 said PeakHour upgraded; it stayed pending."""
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.upgraded(run),
                         ["- upgraded App Store app  434808346  SimpleMind Pro  (2.10.2 -> 2.10.3)"])

    def test_a_line_without_an_id_upgrades_nothing(self):
        """`mas upgrade` with no id takes every app at once, the hang this
        fixes; a line that starts with no id is listed, not upgraded."""
        _stub(self.bin / "mas", 'case "$1" in outdated) printf "%s\\n" "" "Warning: example" ;; esac\n')
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(self.sudo_calls.exists(), self.sudo_calls.read_text() if self.sudo_calls.exists() else "")
        self.assertEqual(self.upgraded(run), [])

    def test_a_failed_check_after_the_upgrades_upgraded_nothing(self):
        """A check that failed lists nothing, which is not 'all upgraded'."""
        run = self.maintenance("# nothing to check\n", OUTDATED_FAILS="1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.upgraded(run), [])
        self.assertIn("- App Store update check failed (mas outdated exit 1)", run.stdout)


if __name__ == "__main__":
    unittest.main()
