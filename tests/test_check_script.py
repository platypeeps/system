"""`tests/check.sh` against stub suites: its lanes and its pack source.

sd:2719: a slot holder exports its cap as `SD_GATE_POOL_SIZE`, and four gates
each running every job at once ran 32 and drove the load past 200. Above a cap
of 1, at most CPUs / cap jobs may run together. sd:2720: the pinned pack came
from GitHub in every gate; the machine's clone answers when it holds the pin.

A copy of `check.sh` runs in a temporary root beside a `ci-native.sh` and a
`run-macos-only.sh` that only record what ran and how many ran together.

Stdlib and git only. This runs in the `tests/ci-native.sh` preflight as
`python3 tests/test_check_script.py` from the repository root, and the
preflight's unwired-suite guard fails when `tests/ci-native.sh` stops naming it.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Each job counts the jobs running beside it as it starts, then holds its
# place for a moment so that a job started next to it is counted.
SUITE = """#!/bin/sh
[ "$1" != preflight ] || exit 0
running="$CI_SYSTEM_ROOT/running"
mkdir -p "$running"
: > "$running/$$"
ls "$running" | wc -l >> "$CI_SYSTEM_ROOT/counts"
sleep 0.5
rm "$running/$$"
echo "$1 ${2:-} ${SUITE_SHARD:-}" >> "$CI_SYSTEM_ROOT/ran"
[ ! -f "$CI_SYSTEM_ROOT/fail-${2:-}" ] || exit 3
"""
MACOS = """#!/bin/sh
if [ "$1" = list ]; then echo 'one sh one.sh'; echo 'two sh two.sh'; exit 0; fi
exec /bin/sh "$(dirname "$0")/ci-native.sh" macos "$1"
"""
JOBS = 5 + (2 if platform.system() == "Darwin" else 0)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class CheckScript(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="check-script-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "root"
        (self.root / "tests").mkdir(parents=True)
        shutil.copy(ROOT / "tests/check.sh", self.root / "tests/check.sh")
        (self.root / "tests/ci-native.sh").write_text(SUITE)
        (self.root / "tests/run-macos-only.sh").write_text(MACOS)
        # The pack: one commit in a clone of its own, pinned.
        self.clone = self.base / "pack-clone"
        self.clone.mkdir()
        git("init", "-q", cwd=self.clone)
        (self.clone / "README").write_text("pack fixture\n")
        git("add", "README", cwd=self.clone)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "-qm", "pack", cwd=self.clone)
        self.pin = git("rev-parse", "HEAD", cwd=self.clone)
        (self.root / ".sd-pack-rev").write_text(self.pin + "\n")
        # check.sh asks for python3.14 by name.
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "python3.14").symlink_to(sys.executable)
        self.cores = os.cpu_count() or 4

    def check(self, *, expect=0, **extra):
        environment = {key: value for key, value in os.environ.items() if key != "SD_GATE_POOL_SIZE"}
        environment.update({"PATH": f"{self.bin}:{os.environ['PATH']}", "SD_PACK_ROOT": str(self.clone),
                            "CI_PACK_URL": str(self.base / "no-such-remote"), **extra})
        done = subprocess.run(["sh", str(self.root / "tests/check.sh"), "run"], capture_output=True,
                              text=True, env=environment, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def counts(self):
        return [int(line) for line in (self.root / "counts").read_text().split()]

    def test_a_pool_above_one_runs_cpus_over_the_pool_at_once(self):
        pool = max(2, self.cores // 2)
        lanes = max(1, self.cores // pool)
        self.assertLess(lanes, JOBS)
        done = self.check(SD_GATE_POOL_SIZE=str(pool))
        self.assertIn(f"check.sh: {JOBS} jobs, {lanes} at a time", done.stdout)
        self.assertEqual(len((self.root / "ran").read_text().splitlines()), JOBS)
        self.assertLessEqual(max(self.counts()), lanes, self.counts())

    def test_no_pool_and_a_pool_of_one_run_every_job_at_once(self):
        for extra in ({}, {"SD_GATE_POOL_SIZE": "1"}, {"SD_GATE_POOL_SIZE": "nope"}):
            with self.subTest(extra=extra):
                done = self.check(**extra)
                self.assertIn(f"check.sh: {JOBS} jobs, {JOBS} at a time", done.stdout)

    def test_a_failing_job_in_a_lane_fails_the_check_and_is_named(self):
        (self.root / "fail-dashboard").touch()
        done = self.check(expect=1, SD_GATE_POOL_SIZE=str(max(2, self.cores // 2)))
        self.assertIn("check.sh: leg-dashboard exited 3", done.stderr)
        self.assertIn("check.sh: failed: leg-dashboard\n", done.stderr)
        # Every other job still ran.
        self.assertEqual(len((self.root / "ran").read_text().splitlines()), JOBS)

    def test_a_failing_run_removes_its_work_dir(self):
        """266 failed runs left /tmp/system-check.* behind (893 MB). Every
        job's log is in the output, and sd-check keeps a failure's whole output."""
        made = self.base / "made"
        (self.bin / "mktemp").write_text(f'#!/bin/sh\nd=$(/usr/bin/mktemp "$@") || exit\necho "$d" >> {made}\necho "$d"\n')
        (self.bin / "mktemp").chmod(0o755)
        (self.root / "fail-dashboard").touch()
        done = self.check(expect=1)
        self.assertIn("leg-dashboard exited 3", done.stderr)
        [work] = made.read_text().split()
        self.assertTrue(work.startswith("/tmp/system-check."), work)
        self.assertFalse(Path(work).exists(), work)

    def test_the_pack_comes_from_the_local_clone_when_it_holds_the_pin(self):
        done = self.check()
        self.assertIn(f"check.sh: fetching the pack from {self.clone}\n", done.stdout)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root / ".ci/pack"), self.pin)

    def test_the_pack_comes_from_the_url_when_the_clone_lacks_the_pin(self):
        other = self.base / "other-clone"
        other.mkdir()
        git("init", "-q", cwd=other)
        done = self.check(SD_PACK_ROOT=str(other), CI_PACK_URL=str(self.clone))
        self.assertIn(f"check.sh: fetching the pack from {self.clone}\n", done.stdout)
        self.assertNotIn(str(other), done.stdout)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root / ".ci/pack"), self.pin)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
