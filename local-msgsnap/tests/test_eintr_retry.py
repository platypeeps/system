"""`msgsnap.sh test`: the CI adapter for `tests/eintr-retry.sh`.

sd:946, the sd:935 follow-up. The shell suite runs the real binary end to
end with open(2) stood in by `interpose_open.c`, and nothing in CI ran it:
the system-native unwired-suite guard keys on `*/tests/test_*.py`, and the
`run_suite` wrapper asserts a unittest summary the shell suite does not
print. This module is that summary. The shell suite stays the single source
of truth for what the binary must do -- every expectation lives there, once,
and the README sends the operator to it -- so this does not restate its
cases. It runs the suite and asserts the line it ends with, and then proves
that assertion can fail: the second case runs a copy of the suite with one
expectation deliberately wrong and asserts the copy is red.

Both cases compile their own binary into a temp dir, as the suite does, and
neither touches `bin/msgsnap`: rebuilding that one changes its cdhash and
kills the FDA grant (README.md, "Gotchas"). Python `unittest` and not sh for
the reason CLAUDE.md gives for the sibling suites: the CI wrapper asserts a
unittest summary and refuses skips. So a runner without a toolchain fails
here with a named reason rather than skipping.
"""

import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE.parent
SUITE = HERE / "eintr-retry.sh"
# What the suite prints last, and what its exit code is, when every case is
# green. The count is pinned so a case dropped from the shell file, or one
# that never ran, is a failure here and not a quieter pass.
SUITE_CASES = 8
# Two compiles (swiftc -O, then clang) and eight runs of a binary that opens
# one file: seconds on the macOS runner. This is the bound past which the
# subprocess is killed and the case fails naming it, rather than holding the
# leg to its own twenty-minute timeout.
RUN_TIMEOUT = 300


def run_suite(script):
    """Run one copy of the suite from a clean cwd and return (rc, output)."""
    # Its own session, so the process group is the script's pid and a hung
    # swiftc or binary under it, which inherits this pipe, is killed with it
    # at the bound (review of #397; the shape of local-health-check's
    # test_sweep.py). `subprocess.run(timeout=)` kills only `sh` and then
    # waits on the pipe the descendant still holds.
    proc = subprocess.Popen(
        ["sh", str(script)], cwd=tempfile.gettempdir(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return proc.returncode, out


class EintrRetrySuite(unittest.TestCase):

    def setUp(self):
        # The suite builds with `swiftc` and `xcrun --sdk macosx clang`. The
        # runner is macos-15, which ships Xcode, but that is asserted here and
        # not assumed: a missing tool is a failure that names itself, never a
        # skip, because a skip would let the leg go green on a runner that
        # ran nothing. Per test and not per class, so the summary still reads
        # `Ran 2 tests` and the CI wrapper reports the named error rather
        # than the absence of a summary.
        for tool in ("swiftc", "xcrun"):
            if shutil.which(tool) is None:
                self.fail(f"{tool} is not on PATH; eintr-retry.sh cannot build the binary it tests")

    def test_the_shell_suite_is_green(self):
        rc, out = run_suite(SUITE)
        self.assertEqual(rc, 0, f"eintr-retry.sh exited {rc}:\n{out}")
        self.assertIn(f"Ran {SUITE_CASES} tests, 0 failed", out.splitlines()[-1:], out)

    def test_a_wrong_expectation_in_the_retry_case_reads_as_red(self):
        """Prove `test_the_shell_suite_is_green` can fail. A copy of the tool
        tree with one expectation changed -- the sd:935 retry case's try
        count, 4 -> 5, at eintr-retry.sh:74-75 -- must run, exit 1 and name
        that case, or the pass above is vacuous."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="msgsnap-eintr-red."))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # The suite finds src/ and tests/ relative to itself, so the copy
        # carries the same shape.
        (tmp / "src").mkdir()
        (tmp / "tests").mkdir()
        shutil.copy(TOOL / "src" / "msgsnap.swift", tmp / "src" / "msgsnap.swift")
        shutil.copy(HERE / "interpose_open.c", tmp / "tests" / "interpose_open.c")
        text = SUITE.read_text()
        pattern = r'^(\s*)3 0 0 4 "ok: readable" --check$'
        self.assertEqual(len(re.findall(pattern, text, flags=re.M)), 1,
                         "the sd:935 retry case is not where this test expects it")
        text = re.sub(pattern, r'\g<1>3 0 0 5 "ok: readable" --check', text, flags=re.M)
        (tmp / "tests" / "eintr-retry.sh").write_text(text)
        rc, out = run_suite(tmp / "tests" / "eintr-retry.sh")
        self.assertEqual(rc, 1, f"the wrong copy exited {rc}, not 1:\n{out}")
        self.assertIn(f"Ran {SUITE_CASES} tests, 1 failed", out.splitlines()[-1:], out)
        self.assertIn("FAIL EINTR three times, then the open succeeds: retried, ok", out)
        self.assertIn("want rc=0 tries=5", out)
        self.assertIn("got  rc=0 tries=4", out)
