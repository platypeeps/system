"""`tests/check.sh` against stub suites: its lanes and its pack source.

sd:2719: a slot holder exports its cap as `SD_GATE_POOL_SIZE`, and four gates
each running every job at once ran 32 and drove the load past 200. Above a cap
of 1, at most CPUs / cap jobs may run together. sd:2720: the pinned pack came
from GitHub in every gate; the machine's clone answers when it holds the pin.

A copy of `check.sh` runs in a temporary root beside a `ci-native.sh` and a
`run-macos-only.sh` that only record what ran and how many ran together.

sd:3534: builders ran a suite by hand against the live pack checkout, which
moves after every pack landing, while the gate runs it against the pin in
`.sd-pack-rev`; three gate failures had passed by hand. `check.sh suite`
(`make test SUITE=<name>`) runs one suite in the gate's environment, and
`run-macos-only.sh` takes the pinned `.ci/pack` instead of the live pack.

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
if [ "$1" = preflight ]; then : > "$CI_SYSTEM_ROOT/preflight-ran"; exit 0; fi
running="$CI_SYSTEM_ROOT/running"
mkdir -p "$running"
: > "$running/$$"
ls "$running" | wc -l >> "$CI_SYSTEM_ROOT/counts"
sleep 0.5
rm "$running/$$"
echo "$1 ${2:-} ${SUITE_SHARD:-} ${SUITE_ONLY:-}" >> "$CI_SYSTEM_ROOT/ran"
[ ! -f "$CI_SYSTEM_ROOT/fail-${2:-}" ] || exit 3
# A suite that reached the real launchctl and still exited 0.
[ ! -f "$CI_SYSTEM_ROOT/leak-${2:-}" ] || launchctl list > /dev/null 2>&1 || :
# A leg that holds the suite SUITE_ONLY names writes its log, as run_suite does.
[ -z "${SUITE_ONLY:-}" ] || [ ! -f "$CI_SYSTEM_ROOT/holds-${2:-}-$SUITE_ONLY" ] || echo ran > "$CI_WORK_ROOT/$SUITE_ONLY.log"
"""
MACOS = """#!/bin/sh
if [ "$1" = list ]; then echo 'one sh one.sh'; echo 'two sh two.sh'; exit 0; fi
exec /bin/sh "$(dirname "$0")/ci-native.sh" macos "$1"
"""
JOBS = 5 + (2 if platform.system() == "Darwin" else 0)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class CheckFixture(unittest.TestCase):
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

    def check(self, *args, expect=0, **extra):
        environment = {key: value for key, value in os.environ.items() if key != "SD_GATE_POOL_SIZE"}
        environment.update({"PATH": f"{self.bin}:{os.environ['PATH']}", "SD_PACK_ROOT": str(self.clone),
                            "CI_PACK_URL": str(self.base / "no-such-remote"), **extra})
        done = subprocess.run(["sh", str(self.root / "tests/check.sh"), *(args or ("run",))], capture_output=True,
                              text=True, env=environment, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def ran(self):
        path = self.root / "ran"
        return [line.split() for line in path.read_text().splitlines()] if path.exists() else []

    def counts(self):
        return [int(line) for line in (self.root / "counts").read_text().split()]


class CheckScript(CheckFixture):
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



class OneSuite(CheckFixture):
    """sd:3534: `check.sh suite <name>`, which `make test SUITE=<name>` runs."""

    def test_a_suite_runs_the_preflight_then_only_that_suite_against_the_pin(self):
        (self.root / "holds-tools-repo-sync").touch()
        done = self.check("suite", "repo-sync")
        self.assertTrue((self.root / "preflight-ran").exists())
        self.assertEqual([["leg", leg, "repo-sync"] for leg in ("shared", "dashboard", "tools")], self.ran())
        self.assertIn("check.sh: suite repo-sync passed", done.stdout)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root / ".ci/pack"), self.pin)

    def test_a_macos_only_suite_runs_through_its_runner(self):
        self.check("suite", "two")
        self.assertEqual([["macos", "two"]], self.ran())

    def test_a_name_no_suite_has_fails_and_says_where_the_names_are(self):
        done = self.check("suite", "nope", expect=1)
        self.assertIn("check.sh: no suite named nope; the run_suite lines in tests/ci-native.sh", done.stderr)
        self.assertNotIn("passed", done.stdout)

    def test_a_failing_suite_fails_the_run(self):
        (self.root / "holds-dashboard-dashboard").touch()
        (self.root / "fail-dashboard").touch()
        done = self.check("suite", "dashboard", expect=3)
        self.assertNotIn("passed", done.stdout)

    def test_a_suite_that_reached_launchctl_fails_as_in_the_gate(self):
        """NEW (codex round): the full run fails a leak a suite swallowed;
        one suite must too."""
        (self.root / "holds-tools-repo-sync").touch()
        (self.root / "leak-tools").touch()
        done = self.check("suite", "repo-sync", expect=1)
        self.assertIn("== launchctl calls that missed their stub", done.stdout)
        self.assertNotIn("passed", done.stdout)

    def test_suite_takes_exactly_one_name(self):
        for args in (("suite",), ("suite", ""), ("suite", "a", "b")):
            with self.subTest(args=args):
                self.check(*args, expect=1)

    def test_make_test_passes_suite_to_check_sh(self):
        run = subprocess.run(["make", "-n", "test", "SUITE=repo-sync"], cwd=ROOT, capture_output=True, text=True,
                             check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual('sh tests/check.sh suite "repo-sync"', run.stdout.strip())


# A stand-in for a folder's test entrypoint: it records that it ran and
# prints the unittest summary run_suite asks for.
ENTRYPOINT = """#!/bin/sh
echo "$0" >> "$CI_WORK_ROOT/entrypoints"
echo 'Ran 1 test in 0.001s'
echo
echo OK
"""


class LegWithSuiteOnly(unittest.TestCase):
    """sd:3534: `ci-native.sh leg` with SUITE_ONLY runs that one run_suite
    line. The temporary root holds only the named folder's entrypoint, so a
    leg that ran any other line fails on the missing file."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ci-native-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root, self.work, pack, bin_dir = base / "root", base / "work", base / "pack", base / "bin"
        for folder in (self.root / "tests", self.work / "venv/bin", pack / "bin", bin_dir):
            folder.mkdir(parents=True)
        shutil.copy(ROOT / "tests/ci-native.sh", self.root / "tests/ci-native.sh")
        for name in ("sd-ship", "sd-docs-lint"):
            (pack / "bin" / name).write_text("")
        (self.work / "venv/bin/python").symlink_to(sys.executable)
        (bin_dir / "python3").symlink_to(sys.executable)
        self.environment = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(base), "CI_SYSTEM_ROOT": str(self.root),
                            "CI_WORK_ROOT": str(self.work), "SD_ACCEPTANCE_PACK": str(pack)}

    def entrypoint(self, path):
        (self.root / path).parent.mkdir(parents=True)
        (self.root / path).write_text(ENTRYPOINT)

    def leg(self, leg, only):
        return subprocess.run(["bash", str(self.root / "tests/ci-native.sh"), "leg", leg], cwd=self.root,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
                              env={**self.environment, "SUITE_ONLY": only})

    def test_only_the_named_suite_of_the_leg_runs(self):
        self.entrypoint("local-repo-sync/repo-sync.sh")
        done = self.leg("tools", "repo-sync")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual("local-repo-sync/repo-sync.sh\n", (self.work / "entrypoints").read_text())
        self.assertTrue((self.work / "repo-sync.log").exists())

    def test_a_leg_without_the_named_suite_runs_nothing(self):
        for leg in ("shared", "dashboard"):
            with self.subTest(leg=leg):
                done = self.leg(leg, "repo-sync")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertFalse((self.work / "entrypoints").exists())
                self.assertFalse((self.work / "repo-sync.log").exists())


# Stand-ins for run-macos-only.sh: uname says Darwin, and python3 stops the
# run at its venv, after the line that names the pack it installs from.
UNAME = "#!/bin/sh\necho Darwin\n"
NO_PYTHON = "#!/bin/sh\nexit 9\n"


class MacosRunnerPack(unittest.TestCase):
    """sd:3534: run by hand, run-macos-only.sh installed sd_db from the live
    pack checkout; it takes the pinned .ci/pack, or SD_ACCEPTANCE_PACK."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="macos-runner-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.home, bin_dir = self.base / "root", self.base / "home", self.base / "bin"
        for folder in (self.root / "tests", bin_dir):
            folder.mkdir(parents=True)
        shutil.copy(ROOT / "tests/run-macos-only.sh", self.root / "tests/run-macos-only.sh")
        (self.root / "tests/macos-only-suites.txt").write_text("one sh one.sh\n")
        for name, body in (("uname", UNAME), ("python3", NO_PYTHON)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.environment = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.home)}
        # The live pack, where the runner used to look, one commit past the pin.
        self.live = self.pack(self.home / "repos/platypeeps/sd-ai-command-pack", commits=2)
        self.pin = git("rev-parse", "HEAD~1", cwd=self.live)
        (self.root / ".sd-pack-rev").write_text(self.pin + "\n")

    @staticmethod
    def pack(path, commits=1):
        (path / "lib").mkdir(parents=True)
        (path / "bin").mkdir()
        (path / "bin/sd-ship").write_text("")
        git("init", "-q", cwd=path)
        for n in range(commits):
            (path / "lib/pyproject.toml").write_text(f"# {n}\n")
            git("add", "-A", cwd=path)
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", f"pack {n}",
                cwd=path)
        return path

    def run_it(self, **extra):
        return subprocess.run(["sh", str(self.root / "tests/run-macos-only.sh"), "one"], capture_output=True,
                              text=True, env={**self.environment, **extra}, stdin=subprocess.DEVNULL, timeout=60)

    def test_it_installs_from_ci_pack_at_the_pin(self):
        ci = self.root / ".ci/pack"
        ci.parent.mkdir()
        git("clone", "-q", str(self.live), str(ci), cwd=self.base)
        git("-c", "advice.detachedHead=false", "checkout", "-q", self.pin, cwd=ci)
        done = self.run_it()
        self.assertIn(f"== installing sd_db from {ci}/lib at {self.pin}", done.stdout, done.stderr)

    def test_with_no_pinned_copy_it_refuses_and_names_make_test(self):
        done = self.run_it()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f".ci/pack is not at the pin in .sd-pack-rev ({self.pin}); run make test SUITE=<suite>",
                      done.stderr)
        self.assertNotIn("installing", done.stdout)

    def test_a_copy_off_the_pin_is_refused(self):
        ci = self.root / ".ci/pack"
        ci.parent.mkdir()
        git("clone", "-q", str(self.live), str(ci), cwd=self.base)
        done = self.run_it()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(".ci/pack is not at the pin", done.stderr)

    def test_sd_acceptance_pack_still_names_the_pack(self):
        done = self.run_it(SD_ACCEPTANCE_PACK=str(self.live))
        self.assertIn(f"== installing sd_db from {self.live}/lib at {git('rev-parse', 'HEAD', cwd=self.live)}",
                      done.stdout, done.stderr)


class ThePostMergeTarget(unittest.TestCase):
    """sd:3568: the lane runs `make post-merge` after a merge (pack sd:3566)."""

    def test_it_refreshes_the_hub_then_the_prompt_then_checks_health(self):
        run = subprocess.run(["make", "-n", "post-merge"], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = [line for line in run.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3, run.stdout)
        for line, tool in zip(lines, ("local-repo-sync/repo-sync.sh refresh",
                                      "local-agent-prompt/agent-prompt.sh refresh --apply",
                                      "local-project-dashboard/dashboard.sh health")):
            with self.subTest(tool=tool):
                self.assertIn(tool, line)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
