"""`msgsnap.sh snap` through launchd waits for the job's exit (sd:8804).

It used to stop at the first byte of output: a diagnostic line, or the
first line of a run still copying, counted as a finished snapshot and
exited 0. launchctl is a stub that runs the plist's program in the
background and answers `print` the way launchd does; the binary is a
script that is denied when run direct, as an ungranted shell is.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

TOOL = pathlib.Path(__file__).resolve().parent.parent

LAUNCHCTL = r"""#!/bin/sh
state="$STUB_STATE"
case "$1" in
  bootstrap)
    plist="$3"
    prog=$(sed -n 's:.*<string>\(.*/bin/msgsnap\)</string>.*:\1:p' "$plist")
    out=$(sed -n 's:.*StandardOutPath</key><string>\(.*\)</string>.*:\1:p' "$plist")
    rm -f "$state"
    ( UNDER_LAUNCHD=1 "$prog" > "$out" 2>&1; echo $? > "$state.tmp"
      mv "$state.tmp" "$state" ) &
    ;;
  print)
    if [ -f "$state" ]; then
      printf '\tstate = not running\n\tlast exit code = %s\n' "$(cat "$state")"
    else
      printf '\tstate = running\n\tlast exit code = (never exited)\n'
    fi
    ;;
esac
exit 0
"""


class SnapViaLaunchd(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.folder = self.root / "local-msgsnap"
        (self.folder / "bin").mkdir(parents=True)
        shutil.copy(TOOL / "msgsnap.sh", self.folder / "msgsnap.sh")
        stubs = self.root / "stubs"
        stubs.mkdir()
        self.write(stubs / "launchctl", LAUNCHCTL)
        self.env = {"PATH": f"{stubs}:{os.environ['PATH']}",
                    "HOME": str(self.root), "TMPDIR": str(self.root),
                    "STUB_STATE": str(self.root / "job-exit")}

    @staticmethod
    def write(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def binary(self, under_launchd):
        self.write(self.folder / "bin" / "msgsnap",
                   '#!/bin/sh\n[ -n "$UNDER_LAUNCHD" ] || exit 2\n' + under_launchd)

    def snap(self):
        return subprocess.run(["sh", str(self.folder / "msgsnap.sh"), "snap"],
                              env=self.env, capture_output=True, text=True,
                              timeout=60)

    def test_diagnostic_output_with_a_failing_exit_is_a_failure(self):
        self.binary('echo "msgsnap: sqlite backup failed"\nexit 5\n')
        done = self.snap()
        self.assertEqual(done.returncode, 5, done.stdout + done.stderr)
        self.assertIn("sqlite backup failed", done.stderr)
        self.assertNotIn("sqlite backup failed", done.stdout)

    def test_waits_for_the_job_to_finish(self):
        self.binary('echo "copying"\nsleep 2\necho "/snap/chat.db  42 bytes"\n')
        done = self.snap()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("/snap/chat.db  42 bytes", done.stdout)


if __name__ == "__main__":
    unittest.main()
