"""fseventsd.sh against a faked pgrep, ps, sysctl, uname and sudo.

Nothing here signals a real process. `sudo` is a stub that records its words
and exits with a code the test picks, so the TERM is observed and never sent;
`pgrep` and `ps` answer from one fixture process table, and `sysctl` answers
the two pressure reads or refuses them. Every test points HOME and
SYSTEM_TOOLS_CONFIG at a temp directory, so nothing reads the real config or
the real state folder, and PATH holds a launchctl stub that fails the test if
called.

Stubs cannot show what the real commands do with a window width, so
TheLiveDaemonReader at the end of this file reads the live daemon through the
real pgrep and ps, on a pipe and on a narrow pseudo-terminal.
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

    def fake_ps(self, rss_kb=None, ghost_pid=None):
        """pgrep and ps over one fixture process table, answering the way the
        real pair does: `pgrep -x NAME` lists the pids whose process name is
        exactly NAME, and `ps -o rss= -p PID` prints that pid's resident
        kilobytes, padded as ps pads them, and refuses a pid it cannot read.
        Either stub refuses any other arguments, so a reader that asks for a
        comm column, or for every process, fails the test instead of reading
        this machine. The table holds launchd, a name that only contains
        `fseventsd`, and the daemon itself when rss_kb is set. ghost_pid is a
        pid pgrep reports and ps cannot read."""
        rows = [("1", "12345", "launchd"),
                ("501", "9876", "a helper with spaces"),
                ("777", "4242", "fseventsd-watcher")]
        if ghost_pid is not None:
            # Before the daemon, so pgrep names it first and a reader that
            # takes the first pid without reading its size is caught.
            rows.append((ghost_pid, "unreadable", "fseventsd"))
        if rss_kb is not None:
            rows.append(("400", str(rss_kb), "fseventsd"))
        table = "".join(f"{pid} {rss} {name}\n" for pid, rss, name in rows)
        table = f"table() {{ cat <<'TABLE'\n{table}TABLE\n}}\n"
        self.stub("pgrep", table + """
[ "$#" -eq 2 ] && [ "$1" = -x ] || { echo "pgrep: unexpected: $*" >&2; exit 2; }
found=$(table | awk -v want="$2" '{ name = $0; sub(/^[0-9]+ [^ ]+ /, "", name)
  if (name == want) print $1 }')
[ -n "$found" ] || exit 1
printf '%s\\n' "$found"
""")
        self.stub("ps", table + """
[ "$#" -eq 4 ] && [ "$1 $2 $3" = "-o rss= -p" ] ||
  { echo "ps: unexpected: $*" >&2; exit 2; }
size=$(table | awk -v want="$4" '$1 == want && $2 ~ /^[0-9]+$/ { print $2 }')
[ -n "$size" ] || exit 1
printf '%8s\\n' "$size"
""")

    def fake_sysctl(self, pressure="1", swap="120.00M"):
        """`pressure` or `swap` None means the read refuses, which is unknown."""
        level = "exit 1" if pressure is None else f"echo {pressure}"
        usage = "exit 1" if swap is None else f"echo '{SWAPUSAGE.format(used=swap)}'"
        self.stub("sysctl", 'case "$*" in\n'
                            f"  *kern.memorystatus_vm_pressure_level*) {level} ;;\n"
                            f"  *vm.swapusage*) {usage} ;;\n"
                            '  *) exit 1 ;;\n'
                            "esac\n")

    def fake_sudo(self, code=0, then=""):
        """Records its words, and the rate-floor marker as it stood when it
        ran, runs `then`, then exits `code`. It never execs anything. `then`
        is the seam for what changes between the TERM and the undo."""
        self.stub("sudo", f"printf '%s\\n' \"$*\" >> '{self.sudo_calls}'\n"
                          f"cat '{self.marker}' >> '{self.sudo_saw}' 2>/dev/null || true\n"
                          f"{then}\n"
                          f"[ {code} -eq 0 ] || echo 'sudo: a password is required' >&2\n"
                          f"exit {code}\n")

    def seal(self, folder):
        """Make `folder` read-only, and writable again at cleanup so the
        temp tree can be removed."""
        folder.mkdir(parents=True, exist_ok=True)
        self.addCleanup(folder.chmod, folder.stat().st_mode)
        folder.chmod(0o500)

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

    # --- status: the size verdict alone decides its exit code -------------

    def test_a_high_size_with_pressure_above_one_exits_1(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("28770 MB resident (pid 400)", done.stdout)
        self.assertIn("over the 4096 MB budget; the machine is hurting", done.stdout)
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

    def test_a_high_size_alone_exits_1_on_a_quiet_machine(self):
        """Replaces test_a_high_size_with_nothing_hurting_exits_0, which
        asserted the folded verdict. The sweep prints nothing for exit 0
        (health-check.sh, `case "$rc" in 0|3) ;;`), so an oversized daemon
        exiting 0 could not be named until the machine was already swapping.
        A daemon over the budget is one nightly line on its own."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="1", swap="120.00M")
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("over the 4096 MB budget; the machine is not hurting",
                      done.stdout)

    def test_a_hurting_machine_with_a_small_daemon_exits_0(self):
        """The other half of the separation: the machine's own trouble is
        reported on the line and never decides this tool's exit code."""
        self.configure()
        self.fake_sysctl(pressure="4", swap="19700.25M")
        done = self.run_fs("status")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("30 MB resident (pid 400), within the 4096 MB budget", done.stdout)
        self.assertIn("the machine is hurting: pressure level 4", done.stdout)

    def test_the_status_line_answers_both_questions_separately(self):
        """Each line carries the size verdict and the machine verdict, so a
        reader of one line can tell which of the two is true."""
        self.configure()
        for rss_mb, pressure, size_say, machine_say in (
                (30, "1", "within the 4096 MB budget", "the machine is not hurting"),
                (30, "4", "within the 4096 MB budget", "the machine is hurting"),
                (28770, "1", "over the 4096 MB budget", "the machine is not hurting"),
                (28770, "4", "over the 4096 MB budget", "the machine is hurting")):
            with self.subTest(rss_mb=rss_mb, pressure=pressure):
                self.fake_ps(rss_kb=rss_mb * 1024)
                self.fake_sysctl(pressure=pressure, swap="120.00M")
                done = self.run_fs("status")
                self.assertEqual(1 if rss_mb == 28770 else 0, done.returncode,
                                 done.stdout + done.stderr)
                self.assertIn(f"{size_say}; {machine_say}", done.stdout)

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

    def test_a_pid_ps_cannot_read_is_skipped_not_sized_at_zero(self):
        """A failed read is an unknown size, never a small one: a pid that
        went away between pgrep and ps must not become a 0 MB daemon that
        reads as within budget."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024, ghost_pid="398")
        done = self.run_fs("status")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("28770 MB resident (pid 400)", done.stdout)
        self.assertNotIn("pid 398", done.stdout)

    def test_a_daemon_only_ps_cannot_read_is_nothing_to_check(self):
        self.configure()
        self.fake_ps(rss_kb=None, ghost_pid="398")
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

    def test_a_daemon_within_budget_is_never_termed_however_bad_the_machine_is(self):
        """The TERM drops the FSEvents history that every backup and indexing
        tool reads, so a 30 MB daemon must survive another process driving the
        machine into swap. Round 1: the folded verdict returned code 0 while
        leaving fs_hurting=yes, and repair's guard read only that, so it
        signalled a healthy daemon once every FSEVENTSD_MIN_HOURS."""
        self.configure()
        for pressure, swap in (("4", "120.00M"), ("1", "19700.25M"),
                               ("4", "19700.25M")):
            with self.subTest(pressure=pressure, swap=swap):
                self.fake_sysctl(pressure=pressure, swap=swap)
                done = self.run_fs("repair")
                self.assertEqual(0, done.returncode, done.stdout + done.stderr)
                self.assertEqual([], self.calls(), "TERMed a daemon within budget")
                self.assertFalse(self.marker.exists())
                self.assertIn("within the 4096 MB budget", done.stdout)

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

    # --- repair: a floor that cannot be recorded refuses to signal --------
    #
    # fs_send_term is called as `fs_send_term || rc=$?`, and an `||` context
    # suppresses errexit for the whole function body, so a setup step whose
    # failure goes unchecked reaches the TERM anyway and reports a floor it
    # never wrote. The next hourly run then finds no floor and signals
    # again, every hour, against the event history every backup and indexing
    # tool reads: the thrash FSEVENTSD_MIN_HOURS exists to stop (round 2).

    def test_a_floor_whose_folder_cannot_be_made_refuses_the_term(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.marker.parent.parent.mkdir(parents=True, exist_ok=True)
        self.marker.parent.write_text("a file where the folder must be\n")
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls(), "TERMed with no floor recorded")
        self.assertIn("no TERM: could not record the floor", done.stdout)

    def test_a_floor_that_cannot_be_written_refuses_the_term(self):
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.seal(self.marker.parent)
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls(), "TERMed with no floor recorded")
        self.assertFalse(self.marker.exists())
        self.assertIn("no TERM: could not record the floor", done.stdout)

    def test_a_clock_that_gives_no_timestamp_refuses_the_term(self):
        """An unread clock would write an empty floor, and every later run
        would read it as garbage and refuse until someone removed it."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.stub("date", "exit 1\n")
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())
        self.assertFalse(self.marker.exists())
        self.assertIn("no TERM: the clock gave no timestamp", done.stdout)

    def test_no_temporary_file_for_sudos_errors_refuses_the_term(self):
        """sudo's stderr is the only account of a refused TERM, and the
        redirection target is made before the floor is written."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        done = self.run_fs("repair", extra={"TMPDIR": str(self.tmp / "gone")})
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual([], self.calls())
        self.assertFalse(self.marker.exists())
        self.assertIn("no TERM: no file for sudo's errors", done.stdout)

    def test_an_undo_that_cannot_be_written_names_the_file_to_remove(self):
        """The TERM failed and the floor could not be undone, so the next
        run is held back by a repair that never happened. Say which file,
        since only a hand can clear it."""
        self.configure()
        self.fake_ps(rss_kb=28770 * 1024)
        self.fake_sysctl(pressure="2", swap="120.00M")
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.marker.parent.chmod, self.marker.parent.stat().st_mode)
        self.fake_sudo(code=1, then=f"chmod 500 '{self.marker.parent}'")
        done = self.run_fs("repair")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertEqual(["-A /usr/bin/pkill -TERM -x fseventsd"], self.calls())
        self.assertIn("could not be undone", done.stdout)
        self.assertIn(str(self.marker), done.stdout)

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

    def test_help_and_readme_declare_the_contract_the_code_runs(self):
        """These codes are the interface the sweep and the hourly job are
        built on, so the declared contract has to be the one in fs_judge:
        status on the size alone, a TERM on both."""
        text = " ".join(self.run_fs("help").stdout.split())
        self.assertIn("Exits 1 when the daemon is over FSEVENTSD_RSS_MB, "
                      "whatever the machine is doing", text)
        self.assertNotIn("and the machine is hurting with it", text)
        readme = " ".join((FOLDER / "README.md").read_text().split())
        self.assertIn("exits 1 on the resident size alone", readme)
        self.assertNotIn("nothing is signalled until both hold", readme.lower())

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

    def test_the_readme_install_lines_make_the_folders_first(self):
        """These lines are run by hand on three machines, where the config
        folders do not exist yet, so a bare cp fails. Every sibling README
        that copies an example into a per-tool folder does the mkdir first."""
        text = (FOLDER / "README.md").read_text()
        block = text.split("## Hourly from launchd", 1)[1]
        block = block.split("```sh", 1)[1].split("```", 1)[0]
        lines = [line for line in block.splitlines() if line.strip()]
        made = [word for line in lines if line.startswith("mkdir -p ")
                for word in line.split()[2:]]
        copies = [line for line in lines if line.startswith("cp ")]
        self.assertTrue(copies, block)
        self.assertTrue(lines[0].startswith("mkdir -p "), lines[0])
        for line in copies:
            target = line.split()[-1]
            folder = target[:-1] if target.endswith("/") else target.rsplit("/", 1)[0]
            self.assertIn(folder, made, f"{folder} is copied into but never made")

    def test_the_env_example_names_every_variable_the_script_reads(self):
        read = set(re.findall(r"\$\{(FSEVENTSD_[A-Z_]+):", ENTRYPOINT.read_text()))
        read.add("FSEVENTSD_RSS_MB")
        example = (FOLDER / ".env.example").read_text()
        named = set(re.findall(r"(?m)^#?(FSEVENTSD_[A-Z_]+)=", example))
        self.assertEqual(read, named)


if __name__ == "__main__":
    unittest.main()
