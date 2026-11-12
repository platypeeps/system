"""fseventsd.sh against a faked ps, sysctl, uname and sudo.

Nothing here signals a real process. `sudo` is a stub that records its words
and exits with a code the test picks, so the TERM is observed and never sent;
`ps` prints a fixture process table, and `sysctl` answers the two pressure
reads or refuses them. Every test points HOME and SYSTEM_TOOLS_CONFIG at a
temp directory, so nothing reads the real config or the real state folder,
and PATH holds a launchctl stub that fails the test if called.
"""

import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "fseventsd.sh"
SUDOERS = FOLDER.parent / "local-machine-setup/machine-setup-repairs.sudoers.example"
JOB = FOLDER.parent / "local-cron-jobs/examples/fseventsd-hourly.job"

#: The daemon's real path, so the stub table is what `ps -axo comm=` prints.
DAEMON = ("/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks"
          "/FSEvents.framework/Versions/A/Support/fseventsd")
#: What macOS prints for vm.swapusage.
SWAPUSAGE = "total = 20480.00M  used = {used}  free = 779.75M  (encrypted)"


class Fseventsd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.config = self.tmp / "config"
        self.home = self.tmp / "home"
        for folder in (self.bin, self.config, self.home):
            folder.mkdir()
        self.marker = self.home / ".local/state/system/fseventsd/last-term"
        self.sudo_calls = self.tmp / "sudo-calls"
        self.sudo_saw = self.tmp / "sudo-saw-marker"
        self.touched = self.tmp / "launchctl-called"
        self.stub("launchctl", f"touch '{self.touched}'\nexit 1\n")
        # The default machine: a healthy daemon on macOS with memory to spare.
        self.fake_uname("Darwin")
        self.fake_ps(rss_kb=30 * 1024)
        self.fake_sysctl(pressure="1", swap="120.00M")
        self.fake_sudo(code=0)

    def tearDown(self):
        self.assertFalse(self.touched.exists(), "fseventsd.sh called launchctl")

    # --- the machine under test ------------------------------------------

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}")
        path.chmod(0o755)

    def fake_uname(self, system):
        self.stub("uname", f"echo {system}\n")

    def fake_ps(self, rss_kb=None):
        """A process table with launchd, a comm holding spaces, a name that
        only contains `fseventsd`, and the daemon itself when rss_kb is set."""
        rows = ["      1   12345 /sbin/launchd",
                "    501    9876 /usr/libexec/a helper with spaces",
                "    777    4242 /usr/local/bin/fseventsd-watcher"]
        if rss_kb is not None:
            rows.append(f"    400 {rss_kb:>7} {DAEMON}")
        table = "\n".join(rows)
        self.stub("ps", f"cat <<'TABLE'\n{table}\nTABLE\n")

    def fake_sysctl(self, pressure="1", swap="120.00M"):
        """`pressure` or `swap` None means the read refuses, which is unknown."""
        level = "exit 1" if pressure is None else f"echo {pressure}"
        usage = "exit 1" if swap is None else f"echo '{SWAPUSAGE.format(used=swap)}'"
        self.stub("sysctl", 'case "$*" in\n'
                            f"  *kern.memorystatus_vm_pressure_level*) {level} ;;\n"
                            f"  *vm.swapusage*) {usage} ;;\n"
                            '  *) exit 1 ;;\n'
                            "esac\n")

    def fake_sudo(self, code=0):
        """Records its words, and the rate-floor marker as it stood when it
        ran, then exits `code`. It never execs anything."""
        self.stub("sudo", f"printf '%s\\n' \"$*\" >> '{self.sudo_calls}'\n"
                          f"cat '{self.marker}' >> '{self.sudo_saw}' 2>/dev/null || true\n"
                          f"[ {code} -eq 0 ] || echo 'sudo: a password is required' >&2\n"
                          f"exit {code}\n")

    def env_file(self, text):
        folder = self.config / "fseventsd"
        folder.mkdir(exist_ok=True)
        (folder / ".env").write_text(text)

    def configure(self, **values):
        values.setdefault("FSEVENTSD_RSS_MB", "4096")
        self.env_file("".join(f"{name}={value}\n" for name, value in values.items()))

    def run_fs(self, *args, extra=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
               "TMPDIR": str(self.tmp), "SYSTEM_TOOLS_CONFIG": str(self.config)}
        env.update(extra or {})
        return subprocess.run(["sh", str(ENTRYPOINT), *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def calls(self):
        if not self.sudo_calls.exists():
            return []
        return self.sudo_calls.read_text().splitlines()

    def write_marker(self, hours_ago=None, text=None):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            text = f"{int(time.time()) - int(hours_ago * 3600)}\n"
        self.marker.write_text(text)

    # --- status: the two conditions --------------------------------------

    def test_a_high_size_with_pressure_above_one_exits_1(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("28770 MB resident (pid 400)", done.stdout)
        self.assertIn("over the 4096 MB budget and the machine is hurting", done.stdout)
        self.assertIn("pressure level 2", done.stdout)

    def test_a_high_size_with_swap_over_the_limit_exits_1(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="1", swap="19700.25M")
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("swap used 19700 MB of 8192 MB", done.stdout)

    def test_swap_in_gigabytes_counts_as_megabytes(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="1", swap="19.24G")
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("swap used 19701 MB of 8192 MB", done.stdout)

    def test_a_high_size_with_nothing_hurting_exits_0(self):
        """One condition is not enough: a big daemon on a machine with memory
        to spare is doing its job, and the TERM drops the FSEvents history."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="1", swap="120.00M")
        done = self.run_fs("status")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("over the 4096 MB budget, machine not hurting", done.stdout)

    def test_a_hurting_machine_with_a_small_daemon_exits_0(self):
        self.configure()
        self.fake_sysctl(pressure="4", swap="19700.25M")
        done = self.run_fs("status")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("30 MB resident (pid 400), within the 4096 MB budget", done.stdout)

    def test_the_first_line_always_starts_with_the_resident_size(self):
        """The trend is visible on a healthy machine too, and the sweep shows
        this line as the finding's remedy text."""
        self.configure()
        for rss_mb, pressure in ((30, "1"), (28770, "1"), (28770, "4")):
            with self.subTest(rss_mb=rss_mb, pressure=pressure):
                self.fake_ps(rss_kb=rss_mb * 1024)
                self.fake_sysctl(pressure=pressure, swap="120.00M")
                done = self.run_fs("status")
                self.assertRegex(done.stdout.splitlines()[0],
                                 rf"^fseventsd: {rss_mb} MB resident \(pid 400\),")

    # --- status: nothing to check and unknown -----------------------------

    def test_no_daemon_exits_3(self):
        self.configure()
        self.fake_ps(rss_kb=None)
        done = self.run_fs("status")
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertIn("nothing to check: no fseventsd process is running", done.stdout)

    def test_a_name_that_only_contains_fseventsd_is_not_the_daemon(self):
        """The fixture table holds /usr/local/bin/fseventsd-watcher."""
        self.configure()
        self.fake_ps(rss_kb=None)
        done = self.run_fs("status")
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertNotIn("4242", done.stdout)

    def test_off_darwin_exits_3(self):
        self.configure()
        self.fake_uname("Linux")
        done = self.run_fs("status")
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertIn("nothing to check: not macOS (uname -s is Linux)", done.stdout)

    def test_unconfigured_exits_3(self):
        """A machine that has not been set up here stays silent in the sweep."""
        done = self.run_fs("status")
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertIn("nothing to check: FSEVENTSD_RSS_MB is not set", done.stdout)

    def test_an_unreadable_pressure_read_is_unknown_not_healthy(self):
        """A failed read gives an unknown result. It is not a no."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure=None, swap=None)
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("whether the machine is hurting is unknown", done.stdout)
        self.assertIn("pressure unreadable, swap unreadable", done.stdout)

    def test_one_unreadable_read_beside_a_quiet_one_is_still_unknown(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="1", swap=None)
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("is unknown: pressure level 1, swap unreadable", done.stdout)

    def test_one_unreadable_read_beside_a_hurting_one_is_hurting(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure=None, swap="19700.25M")
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("the machine is hurting", done.stdout)

    def test_a_bad_setting_is_broken_not_unconfigured(self):
        for name, value in (("FSEVENTSD_RSS_MB", "4g"), ("FSEVENTSD_SWAP_MB", "-1"),
                            ("FSEVENTSD_MIN_HOURS", "six")):
            with self.subTest(name=name):
                self.configure(**{name: value})
                done = self.run_fs("status")
                self.assertEqual(1, done.returncode, done.stdout + done.stderr)
                self.assertIn(f"FAIL: {name} must be whole", done.stdout)

    def test_an_exported_value_counts_without_a_config_file(self):
        done = self.run_fs("status", extra={"FSEVENTSD_RSS_MB": "4096"})
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    # --- repair -----------------------------------------------------------

    def test_repair_sends_no_term_when_nothing_hurts(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="1", swap="120.00M")
        done = self.run_fs("repair")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())
        self.assertFalse(self.marker.exists())

    def test_repair_sends_no_term_on_an_unknown_verdict(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure=None, swap=None)
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())

    def test_repair_sends_no_term_with_no_daemon(self):
        self.configure()
        self.fake_ps(rss_kb=None)
        done = self.run_fs("repair")
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())

    def test_repair_terms_the_daemon_when_both_conditions_hold(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        done = self.run_fs("repair")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(["-A /usr/bin/pkill -TERM -x fseventsd"], self.calls())
        self.assertIn("TERM sent to pid 400", done.stdout)
        self.assertRegex(self.marker.read_text().strip(), r"^\d+$")

    def test_the_attempt_is_recorded_before_the_term(self):
        """A TERM recorded only after it landed would be lost by a crash
        between the two, and the next hourly run would signal again."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        done = self.run_fs("repair")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertRegex(self.sudo_saw.read_text().strip(), r"^\d+$")

    def test_repair_honours_the_rate_floor(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.write_marker(hours_ago=1)
        was = self.marker.read_text()
        done = self.run_fs("repair")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())
        self.assertIn("no TERM: the last was 1h ago, under FSEVENTSD_MIN_HOURS=6", done.stdout)
        self.assertEqual(was, self.marker.read_text())

    def test_repair_terms_again_once_the_floor_has_passed(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.write_marker(hours_ago=7)
        was = self.marker.read_text()
        done = self.run_fs("repair")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(["-A /usr/bin/pkill -TERM -x fseventsd"], self.calls())
        self.assertNotEqual(was, self.marker.read_text())

    def test_a_floor_of_zero_hours_never_holds_a_term_back(self):
        self.configure(FSEVENTSD_MIN_HOURS="0")
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.write_marker(hours_ago=0)
        done = self.run_fs("repair")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(["-A /usr/bin/pkill -TERM -x fseventsd"], self.calls())

    # --- repair: the steps come apart -------------------------------------

    def test_a_failed_term_restores_the_floor_it_had(self):
        """The floor must not hold back the next run for a TERM that never
        happened, so the recorded attempt is undone."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.fake_sudo(code=1)
        self.write_marker(hours_ago=7)
        was = self.marker.read_text()
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual(["-A /usr/bin/pkill -TERM -x fseventsd"], self.calls())
        self.assertIn("the TERM did not go through (exit 1): sudo: a password is required",
                      done.stdout)
        self.assertEqual(was, self.marker.read_text())

    def test_a_failed_first_term_leaves_no_floor_behind(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.fake_sudo(code=1)
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertFalse(self.marker.exists(), "the undone attempt left a floor")

    def test_a_failed_term_lets_the_next_run_try_again(self):
        """The run after a refused sudo: the floor was undone, so it signals."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.fake_sudo(code=1)
        self.assertEqual(1, self.run_fs("repair").returncode)
        self.fake_sudo(code=0)
        done = self.run_fs("repair")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(2, len(self.calls()))

    def test_a_marker_that_holds_no_timestamp_refuses_and_pages(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.write_marker(text="yesterday\n")
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())
        self.assertIn("holds no timestamp; remove it to allow one", done.stdout)

    def test_a_marker_in_the_future_refuses_and_pages(self):
        """A clock moved back must not leave the floor silently closed."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.write_marker(hours_ago=-48)
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())
        self.assertIn("is in the future; remove it to allow one", done.stdout)

    # --- the contract around the tool -------------------------------------

    def test_check_prints_one_line_per_reading_then_the_status_line(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="19700.25M")
        done = self.run_fs("check")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual("uname: Darwin", lines[0])
        self.assertEqual("daemon: pid 400, 28770 MB resident", lines[1])
        self.assertEqual("budget: 4096 MB resident, 8192 MB swap, 6h between TERMs", lines[2])
        self.assertEqual("pressure: 2", lines[3])
        self.assertEqual("swap used: 19700 MB", lines[4])
        self.assertTrue(lines[5].startswith("last TERM: none "), lines[5])
        self.assertTrue(lines[6].startswith("fseventsd: 28770 MB resident"), lines[6])

    def test_help_declares_the_health_check_codes_and_exits_0(self):
        for flag in ("help", "-h", "--help"):
            with self.subTest(flag=flag):
                done = self.run_fs(flag)
                self.assertEqual(0, done.returncode, done.stdout + done.stderr)
                self.assertIn("local-health-check reads these codes",
                              " ".join(done.stdout.split()))

    def test_no_argument_prints_usage_to_stderr_and_exits_1(self):
        done = self.run_fs()
        self.assertEqual(1, done.returncode)
        self.assertEqual("", done.stdout)
        self.assertIn("Usage: fseventsd.sh", done.stderr)

    def test_the_sudoers_example_names_the_exact_command_the_script_runs(self):
        """Evidence from both files, so the two cannot drift: the allowed line
        must carry the words the script hands sudo, and no wildcard."""
        command = re.search(r'^TERM_COMMAND="([^"]+)"$', ENTRYPOINT.read_text(), re.M).group(1)
        self.assertEqual("/usr/bin/pkill -TERM -x fseventsd", command)
        allowed = [line.split("NOPASSWD: ", 1)[1].strip()
                   for line in SUDOERS.read_text().splitlines()
                   if "NOPASSWD: " in line and "pkill" in line]
        self.assertEqual([command], allowed)
        self.assertNotIn("*", command)

    def test_the_hourly_job_runs_repair_and_passes_only_nothing_to_check(self):
        text = JOB.read_text()
        self.assertRegex(text, r'(?m)^JOB_SCHEDULE="\d+ \* \* \* \*"$')
        command = text.split("JOB_COMMAND=", 1)[1]
        self.assertIn("local-fseventsd/fseventsd.sh", command)
        self.assertIn(" repair ", command)
        self.assertIn('-eq 3 ', command)

    def test_the_common_profile_installs_the_hourly_job(self):
        profile = FOLDER.parent / "local-machine-setup/examples/profiles/common.cron"
        entries = [line for line in profile.read_text().splitlines()
                   if line and not line.startswith("#")]
        self.assertIn(JOB.stem, entries)

    def test_the_tool_writes_its_state_outside_the_config_folder(self):
        """Convention 3: the config folder holds values, not what runs write."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.assertEqual(0, self.run_fs("repair").returncode)
        self.assertTrue(self.marker.exists())
        written = [path for path in self.config.rglob("*") if path.is_file()]
        self.assertEqual([self.config / "fseventsd" / ".env"], written)

    def test_the_env_example_names_every_variable_the_script_reads(self):
        read = set(re.findall(r"\$\{(FSEVENTSD_[A-Z_]+):", ENTRYPOINT.read_text()))
        read.add("FSEVENTSD_RSS_MB")
        example = (FOLDER / ".env.example").read_text()
        named = set(re.findall(r"(?m)^#?(FSEVENTSD_[A-Z_]+)=", example))
        self.assertEqual(read, named)


if __name__ == "__main__":
    unittest.main()
