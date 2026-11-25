"""Free space is sampled once per filesystem, and the probe cleans only under pressure (sd:10426).

A review fixture had two configured paths on one filesystem and got two du
runs of up to 600 s each. The 15-minute `probe` samples each filesystem
once, runs no du and no other maintenance while space is healthy, and under
pressure runs only the safe cleanup, under the nightly job's lock, then
records free bytes before and after.
"""

import fcntl

from tests.test_report import Harness, _stub


class DiskProbeTest(Harness):
    def setUp(self):
        super().setUp()
        self.calls = self.tmp / "calls"
        self.calls.write_text("")
        self.used = self.tmp / "df-used"
        self.used.write_text("95\n")
        self.env.update(CALLS=str(self.calls), DF_USED_FILE=str(self.used))
        # 1024-blocks of 1000, Available 1000 - used: what df -Pk prints for a
        # mount point with a space in it; the used percent is read per call.
        _stub(self.bin / "df", 'echo "df $*" >> "$CALLS"\n'
              'u=$(cat "$DF_USED_FILE")\n'
              'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
              'echo "/dev/disk3s5 1000 $((u * 10)) $((1000 - u * 10)) ${u}% /System/Volumes/My Data"\n')
        _stub(self.bin / "du", 'echo "du $*" >> "$CALLS"\n')
        # brew cleanup frees space: after it df reports 80% used.
        _stub(self.bin / "brew", 'echo "brew $*" >> "$CALLS"\n'
              'case "$1" in cleanup) echo 80 > "$DF_USED_FILE" ;; esac\n')
        self.first = self.tmp / "first"
        self.second = self.tmp / "second"
        self.first.mkdir()
        self.second.mkdir()
        self.conf = f"disk|{self.first}|10\ndisk|{self.second}|15\n"
        self.lock_dir = self.tmp / "local-cron-jobs" / "logs"
        self.lock_dir.mkdir(parents=True)

    def logged(self, tool):
        return [line for line in self.calls.read_text().splitlines() if line.split()[0] == tool]

    def test_the_nightly_run_scans_one_filesystem_once(self):
        run = self.maintenance(self.conf)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(len(self.logged("du")), 1, self.calls.read_text())
        # One finding for the filesystem, at the stricter threshold of its two paths.
        self.assertEqual(run.stdout.count("- disk: only 5% free"), 1, run.stdout)
        self.assertIn(f"- disk: only 5% free on {self.first} (threshold 15%)", run.stdout)

    def test_a_healthy_probe_samples_once_and_does_nothing_else(self):
        self.used.write_text("50\n")
        run = self.maintenance(self.conf, "probe")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(len(self.logged("df")), 2, self.calls.read_text())
        self.assertEqual(self.logged("du"), [])
        self.assertEqual(self.logged("brew"), [])
        self.assertFalse((self.tmp / "sudo-calls").exists())
        self.assertEqual(run.stdout.count("disk: "), 1, run.stdout)
        self.assertIn(f"disk: 50% free (512000 bytes) on {self.first} (threshold 15%)", run.stdout)
        self.assertEqual(self.attention(run), [])

    def test_a_probe_under_pressure_cleans_and_records_free_bytes(self):
        run = self.maintenance(self.conf, "probe")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.logged("brew"), ["brew cleanup -s"])
        self.assertEqual(self.logged("du"), [])
        # No OS or App Store update, no Sunday prune: only the safe cleanup.
        self.assertFalse((self.tmp / "sudo-calls").exists())
        self.assertIn(f"- disk: free on {self.first}: 51200 -> 204800 bytes (5% -> 20%, threshold 15%)",
                      run.stdout)
        self.assertEqual(self.attention(run), [])

    def test_a_probe_that_cleans_too_little_needs_attention(self):
        _stub(self.bin / "brew", 'echo "brew $*" >> "$CALLS"\n')
        run = self.maintenance(self.conf, "probe")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(f"- disk: only 5% free on {self.first} after safe cleanup (threshold 15%)", run.stdout)
        self.assertEqual(self.attention(run),
                         [f"SD_REPORT_ATTENTION: maintenance on fixturehost: disk: under 15% free on {self.first}"])

    def test_a_probe_skips_while_the_nightly_run_holds_its_lock(self):
        with open(self.lock_dir / ".maintenance-nightly.flock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run = self.maintenance(self.conf, "probe")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.logged("brew"), [])
        self.assertIn("maintenance-nightly holds its lock", run.stdout)

    def test_a_bad_threshold_or_unknown_path_is_named(self):
        run = self.maintenance(f"disk|{self.first}|ten\n", "probe")
        self.assertIn(f"conf: bad disk threshold in maintenance.conf line 1: disk|{self.first}|ten", run.stdout)
        self.assertEqual(self.logged("df"), [])
        _stub(self.bin / "df", "exit 1\n")
        run = self.maintenance(f"disk|{self.first}|10\n", "probe")
        self.assertIn(f"disk: cannot stat filesystem of {self.first}", run.stdout)
        self.assertEqual(self.logged("brew"), [])
