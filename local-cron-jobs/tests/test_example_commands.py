"""The catalogue's JOB_COMMANDs read their environment when they run, not when loaded.

`cmd_exec` sources the job file (`load_job`) before it sources
`~/.config/shell/env.sh`, and under launchd the plist hands the run only
`HOME` and `PATH`. A `${VAR}` expanded inside a double-quoted JOB_COMMAND is
therefore read before env.sh has set it, and an operator's SD_PACK_ROOT there
never reached agent-meter or shadow-sync (sd:1238, 332d7ec72031). Escaped, the
expansion happens in the `bash -c` that runs the command, after env.sh.

Each example that names SD_PACK_ROOT is found on disk, not listed here, and
run through `cron-jobs.sh exec` the way launchd runs it, with SD_PACK_ROOT
set only in the fixture's env.sh.
"""

import pathlib
import unittest

from tests.test_cron_jobs import FOLDER, Fixture

EXAMPLES = FOLDER / "examples"

# A pack interpreter, `sd` and `sd-ship` that only say they were the ones run.
RECORDER = '#!/bin/sh\nprintf "%s\\n" "$0" >> "$PACK_MARKER"\n'


class PackRootFromEnvShTest(unittest.TestCase):
    def test_every_pack_example_reads_sd_pack_root_from_env_sh(self):
        jobs = sorted(path for path in EXAMPLES.glob("*.job") if "SD_PACK_ROOT" in path.read_text())
        self.assertTrue(jobs, "no example names SD_PACK_ROOT; this test checks nothing")
        for job in jobs:
            with self.subTest(job=job.name):
                fx = Fixture()
                self.addCleanup(fx.destroy)
                pack = fx.tmp / "custom-pack"
                marker = fx.tmp / "pack-ran"
                for relative in (".venv/bin/python", "bin/sd", "bin/sd-ship"):
                    program = pack / relative
                    program.parent.mkdir(parents=True, exist_ok=True)
                    program.write_text(RECORDER)
                    program.chmod(0o755)
                # The working folders the examples name beside the checkout.
                for sibling in ("local-agent-meter",):
                    (fx.tmp / sibling).mkdir(exist_ok=True)
                env_sh = fx.home / ".config" / "shell" / "env.sh"
                env_sh.parent.mkdir(parents=True)
                env_sh.write_text(f'export SD_PACK_ROOT="{pack}"\nexport PACK_MARKER="{marker}"\n')
                fx.write_job(job.stem, job.read_text())
                result = fx.exec_job(job.stem)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                ran = marker.read_text().splitlines() if marker.exists() else []
                self.assertEqual(len(ran), 1, ran)
                self.assertTrue(pathlib.Path(ran[0]).is_relative_to(pack), ran)



class LaneRunJobTest(unittest.TestCase):
    """The lane job is one file for every machine: `sd-ship lane run --hosted` (sd:3003).

    It names no checkout, so a repository moved to another lane host needs no
    new file, and it names no retired flag.
    """

    JOB = EXAMPLES / "lane-run.job"

    def command(self):
        return self.JOB.read_text().split("JOB_COMMAND=", 1)[1]

    def test_the_job_runs_hosted_lanes_and_names_no_checkout_or_retired_flag(self):
        text = self.command()
        self.assertTrue(text.rstrip().endswith('lane run --hosted"'), text)
        for retired in ("-C", "--satellite-only", "change-me"):
            self.assertNotIn(retired, text)

    def test_the_satellite_readme_names_the_job_as_the_way_to_take_requests_in(self):
        readme = (FOLDER.parent / "local-sd-db" / "README.md").read_text()
        offload = readme.split("### Satellite gate offload", 1)[1].split("\n## ", 1)[0]
        self.assertIn("examples/lane-run.job", offload)
        self.assertIn("lane run --hosted", offload)

    def test_the_job_runs_the_pack_sd_ship_with_exactly_those_words(self):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        pack, marker = fx.tmp / "custom-pack", fx.tmp / "pack-ran"
        ship = pack / "bin" / "sd-ship"
        ship.parent.mkdir(parents=True)
        ship.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PACK_MARKER"\n')
        ship.chmod(0o755)
        env_sh = fx.home / ".config" / "shell" / "env.sh"
        env_sh.parent.mkdir(parents=True)
        env_sh.write_text(f'export SD_PACK_ROOT="{pack}"\nexport PACK_MARKER="{marker}"\n')
        fx.write_job(self.JOB.stem, self.JOB.read_text())
        result = fx.exec_job(self.JOB.stem)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["lane run --hosted"])

# A scan-for-secrets.sh that exits with SCAN_RC for `critical`, and records
# each call; `mask` exits 0, so only the record shows it ran.
FAKE_SCAN = """#!/bin/sh
printf '%s\\n' "$1" >> "$SCAN_MARKER"
case $1 in
  mask) exit 0 ;;
  critical) exit "${SCAN_RC:-0}" ;;
esac
exit 64
"""


class SecretScanWeeklyTest(unittest.TestCase):
    """The weekly job scans and pages; it never masks (sd:1254). An
    unattended in-place rewrite can lose a line a session appends between
    the last check and the truncate, so masking stays a step the operator
    runs by hand. The job's exit code is the scan's: 2 pages."""

    def run_job(self, scan_rc):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        scan = fx.tmp / "local-scan-for-secrets" / "scan-for-secrets.sh"
        scan.parent.mkdir()
        scan.write_text(FAKE_SCAN)
        marker = fx.tmp / "scan-calls"
        job = EXAMPLES / "secret-scan-weekly.job"
        fx.write_job(job.stem, job.read_text())
        result = fx.exec_job(job.stem, extra_env={"SCAN_MARKER": str(marker), "SCAN_RC": str(scan_rc)})
        return result, marker.read_text().splitlines() if marker.exists() else []

    def test_the_job_never_masks(self):
        self.assertNotIn("mask", (EXAMPLES / "secret-scan-weekly.job").read_text().split("JOB_COMMAND=", 1)[1])
        result, calls = self.run_job(0)
        self.assertEqual(calls, ["critical"], result.stdout + result.stderr)

    def test_the_exit_code_is_the_scans(self):
        for scan_rc in (0, 2):
            with self.subTest(scan=scan_rc):
                result, _ = self.run_job(scan_rc)
                self.assertEqual(result.returncode, scan_rc, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
