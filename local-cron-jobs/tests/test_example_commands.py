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


# A scan-for-secrets.sh that exits with MASK_RC for `mask` and SCAN_RC for
# `critical`, and records each call.
FAKE_SCAN = """#!/bin/sh
printf '%s\\n' "$1" >> "$SCAN_MARKER"
case $1 in
  mask) exit "${MASK_RC:-0}" ;;
  critical) exit "${SCAN_RC:-0}" ;;
esac
exit 64
"""


class SecretScanWeeklyTest(unittest.TestCase):
    """The weekly job masks, then scans, and a failure in either fails the job
    (sd:1254). A write that failed part way leaves a fresh mtime, so the scan
    reads that file's hits as settling and exits 0; only the mask's own exit
    code then reports the failure."""

    def run_job(self, mask_rc, scan_rc):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        scan = fx.tmp / "local-scan-for-secrets" / "scan-for-secrets.sh"
        scan.parent.mkdir()
        scan.write_text(FAKE_SCAN)
        marker = fx.tmp / "scan-calls"
        job = EXAMPLES / "secret-scan-weekly.job"
        fx.write_job(job.stem, job.read_text())
        result = fx.exec_job(job.stem, extra_env={
            "SCAN_MARKER": str(marker), "MASK_RC": str(mask_rc), "SCAN_RC": str(scan_rc)})
        return result, marker.read_text().splitlines() if marker.exists() else []

    def test_the_exit_code(self):
        cases = {(0, 0): 0, (0, 2): 2, (1, 0): 1, (1, 2): 2}
        for (mask_rc, scan_rc), expected in cases.items():
            with self.subTest(mask=mask_rc, scan=scan_rc):
                result, calls = self.run_job(mask_rc, scan_rc)
                self.assertEqual(calls, ["mask", "critical"], result.stdout + result.stderr)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
