"""`adversarial-gate run`, against a fake `codex` on a private PATH.

sd:790, the cross-repository half of sd:10 criterion 31: requirement 13
named two bugs in `run` that live here and not in the pack. `--timeout`
was parsed into a variable nothing read, so a hung reviewer held the caller
for as long as `codex` cared to; and `--out ""` died inside `${2:?}` with
the shell's own `parameter null or not set` instead of the script's
`run: --out is required`.

Every case puts a `codex` of its own first on a PATH that holds nothing
else but `/usr/bin:/bin`, so no test reaches the real CLI, and the one
happy-path case proves the fake is what was reached by reading what it
recorded. Python `unittest` and not sh for the reason CLAUDE.md gives for
the sibling suites: the CI wrapper asserts a unittest summary and refuses
skips.
"""

import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "adversarial-gate.sh"
# How long a hung fake may hold `run --timeout 1` before the bound is judged
# not to have fired. The fake sleeps far longer than this, so a run that
# comes back inside it came back because the script cut it off.
BOUND_MARGIN = 10
# The outer guard, past which the subprocess is killed and the case fails
# naming the bound rather than hanging the suite with it.
RUN_TIMEOUT = 20
# What `run` exits with when the bound fires: timeout(1)'s own status, so a
# caller can tell a cut-off run from one the reviewer failed itself. The
# README and the script's comment say 124; this pins them.
CUT_OFF = 124
# How long after `run` returns a process of the killed group may still be
# seen. KILL is immediate, but a grandchild whose parent is already dead is
# reaped by launchd on its own schedule, and until then kill -0 finds it.
GONE_MARGIN = 3

REFUSAL = "adversarial-gate: run: --out is required"

# A codex that never answers, and that has the shape of the real one: a
# launcher with a process tree under it. It records that it was reached,
# starts a grandchild in the background and writes that pid, then blocks in
# a foreground grandchild. `sleep` and not `exec sleep`, so the fake itself
# stays the child and the sleeps are its children -- a bound that ends only
# the child leaves both of them running and holding the pipe.
HUNG_CODEX = """printf '%s\\n' "$@" > "$CODEX_ARGV"
sleep 60 &
echo $! > "$CODEX_GRANDCHILD"
sleep 60
"""


class RunAgainstAFakeCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="adversarial-gate-test."))
        self.addCleanup(self._destroy)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.argv_log = self.tmp / "codex.argv"
        self.stdin_log = self.tmp / "codex.stdin"
        self.grandchild = self.tmp / "codex.grandchild"

    def _destroy(self):
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        self.tmp.rmdir()

    def fake_codex(self, body):
        """Put a `codex` of our own first on PATH, and return the PATH to run with."""
        codex = self.bin / "codex"
        codex.write_text("#!/bin/sh\n" + body)
        codex.chmod(0o755)
        # /usr/bin and /bin only: awk, sed, cat, ls and perl for the script,
        # and nothing that could stand in for codex or for the bound.
        return f"{self.bin}:/usr/bin:/bin"

    def run_gate(self, *args, body="exit 0\n"):
        env = {
            "PATH": self.fake_codex(body),
            "HOME": os.environ.get("HOME", str(self.tmp)),
            "CODEX_ARGV": str(self.argv_log),
            "CODEX_STDIN": str(self.stdin_log),
            "CODEX_GRANDCHILD": str(self.grandchild),
        }
        started = time.monotonic()
        # Its own session, so the guard below can kill the whole tree:
        # `subprocess.run(timeout=...)` kills only the `sh` child, and the
        # fakes here leave a `sleep` grandchild behind on purpose.
        proc = subprocess.Popen(
            ["sh", str(SCRIPT), "run", "--lens", "research-brief", *args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            self.fail(f"adversarial-gate run was still running after {RUN_TIMEOUT}s")
        result = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
        return result, time.monotonic() - started

    def test_the_rendered_prompt_reaches_codex_in_a_read_only_sandbox(self):
        # The control: what `run` hands `codex`, read back from the fake. If
        # this fails, the fake is not what was reached and no case below
        # proves anything about the script.
        out = self.tmp / "adversarial.md"
        result, _ = self.run_gate(
            "--repo", str(self.repo), "--out", str(out),
            body='printf "%s\\n" "$@" > "$CODEX_ARGV"\ncat > "$CODEX_STDIN"\n',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.argv_log.read_text().splitlines(),
            ["exec", "-s", "read-only", "-C", str(self.repo), "-o", str(out), "-"],
        )
        core = (HERE.parent / "core.md").read_text()
        first = next(line for line in core.splitlines() if line.strip())
        self.assertIn(first, self.stdin_log.read_text())

    def test_timeout_cuts_off_a_codex_that_never_answers(self):
        # sd:790. Before, `--timeout` was parsed and never applied: this fake
        # sleeps for longer than the outer guard, and the script waited for it.
        # Three things are pinned, each against a way the bound could be
        # faked (review of #396): codex was reached, so a script that refused
        # the flag before running anything does not pass; the exit is the
        # documented cut-off status and not whatever a validation failure
        # exits with; and the fake's grandchild is gone, so a bound that
        # ended only the launcher and left its tree running does not pass.
        out = self.tmp / "adversarial.md"
        result, elapsed = self.run_gate(
            "--repo", str(self.repo), "--out", str(out), "--timeout", "1",
            body=HUNG_CODEX,
        )
        self.assertTrue(self.argv_log.exists(), "codex was never reached: " + result.stderr)
        self.assertEqual(result.returncode, CUT_OFF, result.stderr)
        self.assertIn("did not answer within 1 s", result.stderr)
        self.assertLess(
            elapsed, BOUND_MARGIN,
            f"run --timeout 1 took {elapsed:.1f}s: the bound did not fire",
        )
        pid = int(self.grandchild.read_text())
        deadline = time.monotonic() + GONE_MARGIN
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail(f"the fake's grandchild {pid} survived the bound: only the launcher was ended")

    def test_an_empty_out_is_refused_with_the_scripts_own_message(self):
        # sd:790. Before, `--out ""` died inside `${2:?}` with the shell's
        # `2: parameter null or not set`, and the exit status was the shell's
        # too. Nothing about that told the caller which flag was wrong.
        result, _ = self.run_gate("--repo", str(self.repo), "--out", "")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(REFUSAL, result.stderr)
        self.assertNotIn("parameter null or not set", result.stderr)
        self.assertFalse(self.argv_log.exists(), "codex was reached without an output path")

    def test_a_missing_out_is_refused_with_the_scripts_own_message(self):
        result, _ = self.run_gate("--repo", str(self.repo))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(REFUSAL, result.stderr)
        self.assertFalse(self.argv_log.exists(), "codex was reached without an output path")

    def test_out_with_no_value_names_the_flag(self):
        # The flag as the last word: not the same fault as an empty value,
        # and not the shell's `2: parameter not set` either.
        result, _ = self.run_gate("--repo", str(self.repo), "--out")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("adversarial-gate: run: --out needs a value", result.stderr)
        self.assertFalse(self.argv_log.exists())

    def test_a_timeout_that_is_not_seconds_is_refused(self):
        out = self.tmp / "adversarial.md"
        for value in ("", "0", "soon", "1.5"):
            with self.subTest(timeout=value):
                result, _ = self.run_gate(
                    "--repo", str(self.repo), "--out", str(out), "--timeout", value,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("adversarial-gate: run: --timeout wants seconds", result.stderr)
                self.assertFalse(self.argv_log.exists())


if __name__ == "__main__":
    unittest.main()
