"""Regression tests for cron-jobs.sh.

Each case runs a throwaway copy of the script against a job file of its own,
with HOME pointed at an empty directory (so no `~/.config/shell/env.sh` is
sourced), CLAUDE_BIN pointed at a fake that records what it was handed, and
SD_REPORT_BIN pointed at `true` so no run report reaches the real database.
`cmd_exec` is what launchd invokes, so it is what these exercise -- through
`exec`, the way the plist does.

REGRESSION means the case fails against the code from before its fix, which
is checkable with CRON_JOBS_TEST_SCRIPT and is the only thing that makes such
a test evidence:

    CRON_JOBS_TEST_SCRIPT=$(git show aa5153e:local-cron-jobs/cron-jobs.sh ...)

PIN means it records a decision that was deliberate and could be undone by
accident; a PIN passing against old code is expected, not a defect.
"""

import os
import pathlib
import plistlib
import pty
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
import unittest.mock

FOLDER = pathlib.Path(__file__).resolve().parent.parent

# The launchd label prefix the script uses when SYSTEM_TOOLS_LABEL_PREFIX is
# unset, which is every run in this suite unless a test sets it.
LABEL_PREFIX = "local.system-tools"
# A case arm for cron labels in health-check.sh, whatever spells the prefix: a
# literal (`local.system-tools.cron.*)`) or an expansion
# (`"$LABEL_PREFIX".cron.*)`). The _EOL form is the arm that ends its line.
CRON_LABEL_ARM = re.compile(r"[^\s(]*\.cron\.\*\)")
CRON_LABEL_ARM_EOL = re.compile(r"[^\s(]*\.cron\.\*\)\n")
SCRIPT = pathlib.Path(os.environ.get("CRON_JOBS_TEST_SCRIPT") or FOLDER / "cron-jobs.sh")
LIB_CONFIG = FOLDER.parent / "lib" / "config.sh"

# The fake agent: one file for argv and one for the environment it was
# started with, NUL-separated so a value holding a newline cannot split. It
# writes the file `--debug-file` names, as the real binary does, and exits
# with FAKE_CLAUDE_EXIT (0 when unset), so a failing run can be staged.
# FAKE_CLAUDE_SAY, when set, is its reply on stdout.
FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CLAUDE_LOG.argv"
[ -z "$FAKE_CLAUDE_SAY" ] || printf '%s\\n' "$FAKE_CLAUDE_SAY"
env -0 > "$FAKE_CLAUDE_LOG.env"
while [ $# -gt 0 ]; do
  [ "$1" != "--debug-file" ] || echo "fake trace" > "$2"
  shift
done
exit "${FAKE_CLAUDE_EXIT:-0}"
"""


class Fixture:
    """A disposable copy of cron-jobs.sh with its own config dir, logs/ and HOME.

    The script resolves logs relative to wherever it sits and sources
    ../lib/config.sh, so the copy is laid out the way the tree is. Jobs live
    in <config>/cron-jobs/jobs, with SYSTEM_TOOLS_CONFIG pointed at a
    directory of this fixture's own, never the real ~/.config.
    """

    def __init__(self, script=SCRIPT):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="cron-jobs-test."))
        self.folder = self.tmp / "local-cron-jobs"
        self.folder.mkdir()
        (self.folder / "logs").mkdir()
        shutil.copy(script, self.folder / "cron-jobs.sh")
        (self.tmp / "lib").mkdir()
        shutil.copy(LIB_CONFIG, self.tmp / "lib" / "config.sh")
        self.config = self.tmp / "config"
        self.conf_dir = self.config / "cron-jobs"
        self.jobs = self.conf_dir / "jobs"
        self.jobs.mkdir(parents=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.claude = self.tmp / "claude"
        self.claude.write_text(FAKE_CLAUDE)
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        self.claude_log = self.tmp / "claude-call"
        # `notify_failure` calls bare `osascript`, so every staged failure
        # posted a real desktop notification: the suite fired 9 of them at
        # the user per run, measured with a recording stub on PATH. The stub
        # goes on the fixture rather than beside `launchctl` in `stub_bin`,
        # because the two PATHs the runs use are built in two places --
        # `exec_job` below and `stub_env` -- and only the second one consults
        # `stub_bin`. Four of the nine came through the first. It records
        # what it was handed, so a test can assert the call landed here and
        # the stub cannot go stale unnoticed.
        self.osascript_log = self.tmp / "osascript-calls.txt"
        self.quiet = self.tmp / "quiet-bin"
        self.quiet.mkdir()
        osascript = self.quiet / "osascript"
        osascript.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" >> '{self.osascript_log}'\n"
            "exit 0\n")
        osascript.chmod(osascript.stat().st_mode | stat.S_IXUSR)

    def destroy(self):
        # sd:813. Removing the directory does not release a process blocked
        # opening one of its FIFOs, and nothing else ever will. A timeout in
        # exec_job kills only the shell it started, so every failing run of
        # HandRunMirrorTest's lost-EOF case left its fake tee's two processes
        # running for good. Whatever this fixture started is killed here, and
        # the test that left it fails, whether it passed or not.
        try:
            survivors = self.kill_survivors()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)
        if survivors:
            raise AssertionError(
                "processes started from this fixture outlived the test and were killed:\n"
                + "\n".join(f"{pid} {command}" for pid, command in survivors))

    def processes(self):
        """(pid, command) of every process whose command line names this fixture's directory.

        That is how a leftover is told from anything else on the machine: the
        directory is mkdtemp's, so no process started elsewhere names it, and
        it is matched with its trailing slash, so a sibling whose name merely
        begins with it is not taken. A process whose command line does not
        name the directory is not found.
        """
        roots = {f"{self.tmp}/", f"{os.path.realpath(self.tmp)}/"}
        listing = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                                 capture_output=True, text=True, check=True).stdout
        found = []
        for line in listing.splitlines():
            pid, _, command = line.strip().partition(" ")
            if any(root in command for root in roots):
                found.append((int(pid), command))
        return found

    def kill_survivors(self, grace=5.0):
        """SIGKILL, by pid, each process `processes` finds, until it finds none.

        Listed again after every round, because a process can start another
        between a listing and its kill: the fake tee forks the reader that
        blocks, and a single listing missed that child in a run at sd:813.
        Returns every process it killed.
        """
        killed = {}
        deadline = time.monotonic() + grace
        while left := self.processes():
            if time.monotonic() > deadline:
                raise AssertionError(f"fixture processes still running {grace} s after SIGKILL: {left}")
            for pid, command in left:
                killed.setdefault(pid, command)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.05)
        return list(killed.items())

    def path(self, *front):
        """A PATH with `front` first, this fixture's quiet stubs next, and the
        machine's own PATH last. Every run started from this fixture goes
        through here, so nothing reaches the real `osascript`."""
        return ":".join([*(str(item) for item in front), str(self.quiet),
                         os.environ.get("PATH", "/usr/bin:/bin")])

    def notifications(self):
        """The `display notification` calls this fixture's runs made."""
        if not self.osascript_log.exists():
            return []
        return [line for line in self.osascript_log.read_text().splitlines()
                if "display notification" in line]

    def write_job(self, name, text):
        (self.jobs / f"{name}.job").write_text(text)

    def exec_job(self, name, extra_env=None, timeout=60):
        env = {
            "PATH": self.path(),
            "HOME": str(self.home),
            "SYSTEM_TOOLS_CONFIG": str(self.config),
            "CLAUDE_BIN": str(self.claude),
            "FAKE_CLAUDE_LOG": str(self.claude_log),
            # `true reports ingest ...` exits 0 and records nothing; without
            # this a real `sd` on PATH would file the run in the shared db.
            "SD_REPORT_BIN": "/usr/bin/true",
        }
        env.update(extra_env or {})
        try:
            return subprocess.run(
                ["sh", str(self.folder / "cron-jobs.sh"), "exec", name],
                env=env, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # A bare TimeoutExpired is what CI showed for sd:773, and it said
            # nothing about where the run stood. Its partial output does.
            def text(raw):
                return raw.decode(errors="replace") if isinstance(raw, bytes) else raw
            raise AssertionError(
                f"cron-jobs.sh exec {name} did not finish in {timeout}s\n"
                f"stdout so far: {text(exc.stdout)!r}\nstderr so far: {text(exc.stderr)!r}"
            ) from None

    def claude_env(self):
        raw = (self.tmp / "claude-call.env").read_bytes().decode()
        return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)

    def claude_argv(self):
        return (self.tmp / "claude-call.argv").read_text().splitlines()

    def claude_was_called(self):
        return (self.tmp / "claude-call.argv").exists()


PROMPT_JOB = 'JOB_SCHEDULE="0 3 * * *"\nJOB_PROMPT="/sd-plan nightly"\n'

# A workload that SIGKILLs the runner, the shell `cron-jobs.sh exec` is. Not
# `$PPID`: the workload's parent is the perl that enforces JOB_TIMEOUT, and the
# runner is that perl's parent (sd:2018).
KILL_THE_RUNNER = "kill -9 $(ps -o ppid= -p $PPID)"


class PromptJobEnvironmentTest(unittest.TestCase):
    """The environment a prompt job's `claude -p` is started with."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)

    def test_prompt_job_disables_handoff_restore(self):
        # REGRESSION. The pack's SessionStart hook (bin/sd-handoff-restore)
        # restores a pending handoff packet into any session that starts in
        # the repository unless SD_HANDOFF_RESTORE=0 is in its environment,
        # and its SKILL.md has said all along that cron-jobs.sh exports that
        # for every unattended -p job. Nothing here did, so an overnight job
        # standing in a repository with a packet consumed it.
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fx.claude_env().get("SD_HANDOFF_RESTORE"), "0")
        self.assertEqual(self.fx.claude_argv()[:2], ["-p", "/sd-plan nightly"])

    def test_prompt_job_overrides_an_inherited_value(self):
        # REGRESSION. launchd's environment is the plist's, but a hand `run`
        # inherits the operator's shell; a stray value there must not reach
        # the job either.
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"SD_HANDOFF_RESTORE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fx.claude_env().get("SD_HANDOFF_RESTORE"), "0")

    def test_command_job_is_not_touched(self):
        # PIN. The variable is set on the agent call, not exported for the
        # run: a JOB_COMMAND job never starts claude here and inherits
        # nothing from this.
        marker = self.fx.tmp / "command-env"
        self.fx.write_job(
            "plain",
            f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="env > {marker}"\n',
        )
        result = self.fx.exec_job("plain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SD_HANDOFF_RESTORE=", marker.read_text())
        self.assertFalse(self.fx.claude_was_called())


class PromptJobResultLineTest(unittest.TestCase):
    """JOB_RESULT_OK: a prompt job judged by its reply's last RESULT: line.

    `claude -p` exits 0 whatever the prompt concluded, so a prompt that
    reports failure in words passed as a green run.
    """

    JOB = PROMPT_JOB + 'JOB_RESULT_OK="^RESULT: (ok|skipped, window closed)$"\n'

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("nightly", self.JOB)

    def run_saying(self, reply):
        return self.fx.exec_job("nightly", {"FAKE_CLAUDE_SAY": reply})

    def test_a_matching_result_line_passes(self):
        result = self.run_saying("did things\nRESULT: ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("did things", result.stdout)

    def test_a_failed_result_line_fails_the_run(self):
        result = self.run_saying("RESULT: ok\nRESULT: failed, step 3: portal login")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("RESULT: failed, step 3: portal login does not match", result.stdout)

    def test_a_missing_result_line_fails_the_run(self):
        result = self.run_saying("I stopped early")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no RESULT: line", result.stdout)

    def test_an_agent_failure_keeps_its_own_exit_code(self):
        result = self.fx.exec_job("nightly", {"FAKE_CLAUDE_SAY": "RESULT: ok", "FAKE_CLAUDE_EXIT": "7"})
        self.assertEqual(result.returncode, 7, result.stdout)

    def test_without_the_setting_the_reply_is_not_judged(self):
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.run_saying("RESULT: failed, step 1: x")
        self.assertEqual(result.returncode, 0, result.stdout)


class PromptJobDebugFileTest(unittest.TestCase):
    """The per-run debug file a prompt job's `claude -p` writes (sd:972).

    A `claude -p` that dies inside the runtime before it reaches a session
    prints one line the runner cannot read past ("error: An unknown error
    occurred (Unexpected)") and writes no debug log unless asked. The runner
    asks, per run, and keeps the trace only when the run failed.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.logs = self.fx.folder / "logs"

    def stub_path(self, name, text="#!/bin/sh\nexit 0\n"):
        """A PATH with a stub `name` first on it, and return that PATH."""
        bin_dir = self.fx.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / name
        stub.write_text(text)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        return self.fx.path(bin_dir)

    def quiet_path(self):
        """A PATH whose `osascript` does nothing, so a staged failure posts no notification."""
        return self.stub_path("osascript")

    def agent_line(self, result):
        """The one `agent:` line of the job log, which stdout mirrors."""
        log = (self.logs / "nightly.log").read_text()
        lines = [line for line in log.splitlines() if "] " in line and " agent: " in line]
        self.assertEqual(len(lines), 1, f"the job log does not carry one agent: line:\n{log}")
        self.assertIn(lines[0], result.stdout)
        return lines[0]

    def debug_file_argument(self):
        argv = self.fx.claude_argv()
        self.assertIn("--debug-file", argv, argv)
        return pathlib.Path(argv[argv.index("--debug-file") + 1])

    def debug_files(self):
        return sorted(self.logs.glob("nightly.debug.*.log"))

    def test_prompt_job_names_a_debug_file_under_the_log_dir(self):
        # REGRESSION (sd:972). Nothing asked the binary for its trace, so the
        # runner had one line from the callee and nothing else.
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.debug_file_argument()
        self.assertEqual(path.parent.resolve(), self.logs.resolve(), path)
        self.assertRegex(path.name, r"^nightly\.debug\.\d{8}T\d{6}Z-\d+\.log$")

    def test_the_log_names_the_binary_and_what_it_links_to(self):
        # REGRESSION (sd:972). ~/.local/bin/claude is a symlink the installer
        # moves between versions/, and the log said only the cwd, so which
        # binary a failed run used was not recoverable. The fake is reached
        # through a link here, so the target is a different path from the one
        # CLAUDE_BIN names and both halves are checked.
        link = self.fx.tmp / "claude-link"
        link.symlink_to(self.fx.claude)
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"CLAUDE_BIN": str(link)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            self.agent_line(result),
            rf"^\[nightly\] \S+ agent: {re.escape(str(link))} -> {re.escape(os.path.realpath(self.fx.claude))}$")

    def test_the_log_falls_back_to_the_binary_path_when_readlink_fails(self):
        # PIN (sd:972). `readlink -f` is not on every platform's readlink; a
        # binary that cannot be resolved is still named, as itself.
        path = self.stub_path("readlink", "#!/bin/sh\nexit 1\n")
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"PATH": path})
        self.assertEqual(result.returncode, 0, result.stderr)
        claude = re.escape(str(self.fx.claude))
        self.assertRegex(self.agent_line(result), rf"^\[nightly\] \S+ agent: {claude} -> {claude}$")

    def test_command_job_gets_no_debug_file(self):
        # PIN. The flag is the agent call's; a JOB_COMMAND job never starts
        # claude here and its log dir gains nothing.
        marker = self.fx.tmp / "command-env"
        self.fx.write_job(
            "plain",
            f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="env > {marker}"\n',
        )
        result = self.fx.exec_job("plain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.fx.claude_was_called())
        self.assertNotIn("debug-file", marker.read_text())
        self.assertEqual(list(self.logs.glob("*.debug.*")), [])
        self.assertNotIn(" agent: ", (self.logs / "plain.log").read_text())

    def test_a_failed_run_keeps_its_debug_file_and_a_passed_run_removes_it(self):
        # REGRESSION (sd:972). Only a failure is worth a trace; a passing run
        # four times a day would otherwise leave one each time.
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"FAKE_CLAUDE_EXIT": "1", "PATH": self.quiet_path()})
        self.assertEqual(result.returncode, 1, result.stderr)
        kept = self.debug_file_argument()
        self.assertTrue(kept.exists(), f"a failed run's debug file is gone: {kept}")
        self.assertEqual(self.debug_files(), [kept])

        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        removed = self.debug_file_argument()
        self.assertFalse(removed.exists(), f"a passed run's debug file stayed: {removed}")
        self.assertEqual(self.debug_files(), [kept])

    def test_a_run_keeps_the_newest_five_debug_files(self):
        # REGRESSION (sd:972). Bounded, or a job that fails every night grows
        # the log dir without end.
        self.fx.write_job("nightly", PROMPT_JOB)
        now = int(time.time())
        older = []
        for n in range(6):
            path = self.logs / f"nightly.debug.2026091{n}T030000Z-{100 + n}.log"
            path.write_text("old trace\n")
            os.utime(path, (now - 3600 * (6 - n), now - 3600 * (6 - n)))
            older.append(path)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.debug_files(), sorted(older[1:]))


# A tee that misses the EOF of its first FIFO, as tee on macOS did in sd:773:
# once the writer has closed, it is blocked on the FIFO again and stays there
# until something opens it for writing. The real miss is a race, about one run
# in three hundred; this makes it every run.
LOSES_EOF_TEE = """#!/bin/sh
/usr/bin/tee "$@"
for fifo in "$2".fifo.*; do
  /usr/bin/tee "$@" < "$fifo" > /dev/null
done
"""

# How long the reopen below is given before it counts as blocked. The shipped
# form returns in microseconds; the write-only form never returns at all, so
# this is only how long the negative control costs the suite.
REOPEN_BUDGET = 2.0


class HandRunMirrorTest(unittest.TestCase):
    """The tee a hand-run mirrors its output through."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)

    def fake_tee(self, text):
        """Put a tee of our own first on PATH, and return the PATH to run with."""
        bin_dir = self.fx.tmp / "bin"
        bin_dir.mkdir()
        tee = bin_dir / "tee"
        tee.write_text(text)
        tee.chmod(tee.stat().st_mode | stat.S_IXUSR)
        return f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    def reopen_line(self):
        """stop_tee's reopen of the FIFO, lifted from the script under test.

        Read rather than restated, and read from the body of `stop_tee` only,
        from `stop_tee() {` to its closing `}`. A search of the whole file
        would take a leftover copy of the line anywhere else in it, and then
        pass while stop_tee itself opened the FIFO write-only. Every line of
        the body that names the FIFO is taken except its `rm -f` and the reset
        at the end, so a reopen rewritten into another shape is still the line
        that gets run.
        """
        text = SCRIPT.read_text().splitlines()
        starts = [n for n, line in enumerate(text) if line.startswith("stop_tee() {")]
        self.assertEqual(len(starts), 1, f"stop_tee is not defined once: {starts}")
        ends = [n for n in range(starts[0] + 1, len(text)) if text[n] == "}"]
        self.assertTrue(ends, "stop_tee has no closing brace in column one")
        body = text[starts[0] + 1:ends[0]]
        lines = [line.strip() for line in body
                 if "$TEE_FIFO" in line
                 and not line.strip().startswith(("rm ", "TEE_PID="))]
        self.assertEqual(len(lines), 1, f"stop_tee's reopen is no longer one line: {lines}")
        return lines[0]

    def returns_on_a_readerless_fifo(self, name, line):
        """Run `line` with TEE_FIFO on a FIFO nothing is reading. True if it returns."""
        fifo = self.fx.tmp / f"{name}.fifo"
        os.mkfifo(fifo)
        self.addCleanup(fifo.unlink)
        try:
            subprocess.run(["sh", "-c", f'TEE_FIFO="$1"; {line}', "sh", str(fifo)],
                           capture_output=True, text=True, timeout=REOPEN_BUDGET)
            return True
        except subprocess.TimeoutExpired:
            return False

    def test_the_fifo_is_reopened_read_write_and_not_write_only(self):
        # PIN (sd:808). `1<>` is O_RDWR and it is load-bearing: opening a FIFO
        # write-only blocks until a reader arrives, and tee can exit between
        # stop_tee's `kill -0` and its open, leaving none. `1>` there would
        # turn sd:773's rare lost wakeup into a certain hang.
        #
        # Not asserted through a whole run, and that is deliberate. A run
        # cannot tell the two apart: `wait "$TEE_PID"` makes every run last at
        # least as long as tee's process, and that process's exit sends the
        # shell a SIGCHLD which breaks a blocked open out with EINTR
        # ("Interrupted system call", measured). So a fixture tee that lingers
        # long enough for the window to be certain also rescues the write-only
        # open from it, and both forms return together. What is left is the
        # open itself, run here on a FIFO that never has a reader.
        line = self.reopen_line()
        self.assertTrue(
            self.returns_on_a_readerless_fifo("shipped", line),
            f"stop_tee's reopen blocks when nothing is reading the FIFO: {line}")
        # The negative control. Without it the case above passes for the wrong
        # reason the moment this fixture grows a reader.
        self.assertFalse(
            self.returns_on_a_readerless_fifo("write-only", line.replace("1<>", "1>")),
            "the negative control failed: the lifted line with `1<>` changed to "
            f"`1>` returned within {REOPEN_BUDGET} s while TEE_FIFO named a FIFO "
            "that nothing reads. Either the line does not open that FIFO or "
            "something is reading it; either way the case above no longer tells "
            f"read-write from write-only. Line run: {line.replace('1<>', '1>')}")

    def test_a_lost_eof_does_not_hang_the_run(self):
        # REGRESSION (sd:773). stop_tee closed the FIFO and waited on tee with
        # nothing else, so a tee that never saw EOF held the run open until
        # the test's timeout, then 60 seconds for a 0.2 second job; this case
        # bounds it at 20.
        path = self.fake_tee(LOSES_EOF_TEE)
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"PATH": path}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[nightly]", result.stdout)
        log = (self.fx.folder / "logs" / "nightly.log").read_text()
        self.assertRegex(log, r"\[nightly\] \S+ done")
        self.assertEqual(list((self.fx.folder / "logs").glob("*.fifo.*")), [])


class FixtureCleanupTest(unittest.TestCase):
    """What Fixture.destroy does with a process a test left running."""

    def blocked_reader(self, directory):
        """Start a shell blocked opening a FIFO in `directory` that nothing writes."""
        fifo = directory / "never-written.fifo"
        os.mkfifo(fifo)
        proc = subprocess.Popen(["sh", "-c", 'exec cat < "$1"', "sh", str(fifo)])
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def test_destroy_kills_what_outlived_the_test_and_fails_it(self):
        # REGRESSION (sd:813). The shape a failing lost-EOF run leaves: a
        # shell blocked for good opening a FIFO under the fixture. destroy
        # used to remove the directory around it and report nothing. The
        # decoy's directory begins with the fixture's name and is not under
        # it, so it pins that only the fixture's own processes are killed.
        fx = Fixture()
        self.addCleanup(shutil.rmtree, fx.tmp, ignore_errors=True)
        decoy_dir = pathlib.Path(f"{fx.tmp}-decoy")
        decoy_dir.mkdir()
        self.addCleanup(shutil.rmtree, decoy_dir, ignore_errors=True)
        left = self.blocked_reader(fx.tmp)
        decoy = self.blocked_reader(decoy_dir)
        with self.assertRaisesRegex(AssertionError, rf"(?m)^{left.pid} sh -c ") as raised:
            fx.destroy()
        self.assertNotRegex(str(raised.exception), rf"(?m)^{decoy.pid} ")
        self.assertEqual(left.wait(timeout=5), -signal.SIGKILL)
        self.assertIsNone(decoy.poll(), "destroy killed a process outside its fixture")
        self.assertFalse(fx.tmp.exists())

    def test_destroy_kills_a_child_forked_after_the_first_listing(self):
        # A process can start another between a listing and its kill, as the
        # fake tee forks its blocked reader (sd:813). The second listing finds
        # the child; one listing would kill only the parent.
        fx = Fixture()
        self.addCleanup(shutil.rmtree, fx.tmp, ignore_errors=True)
        listings = iter([[(101, "parent")], [(202, "forked child")], []])
        killed = []
        with unittest.mock.patch.object(fx, "processes", lambda: next(listings)), \
                unittest.mock.patch("os.kill", lambda pid, sig: killed.append((pid, sig))):
            self.assertEqual(fx.kill_survivors(), [(101, "parent"), (202, "forked child")])
        self.assertEqual(killed, [(101, signal.SIGKILL), (202, signal.SIGKILL)])


class StatusExitCodeTest(unittest.TestCase):
    """`status` answers with CLAUDE.md convention 6 codes, read from the log.

    REGRESSION (sd:1201). local-health-check read launchd's `last exit code`
    for <prefix>.cron.* labels. That counter moves only when launchd itself
    spawns a run, so three weekly and nightly jobs
    all reported exit 1 while their own logs ended in "done": each had failed
    at its slot and been re-run by hand, and a hand-run cannot clear the
    counter. The job log records every run through either path, so the verdict
    is taken from it. `status` used to exit 0 whatever the logs said.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        self.calls = self.fx.tmp / "launchctl-calls"
        self.calls.touch()

    def install(self, job="demo"):
        """The one thing that marks a job installed here: its plist."""
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"local.system-tools.cron.{job}.plist").write_text("<plist/>\n")

    def log(self, lines, job="demo"):
        (self.fx.folder / "logs" / f"{job}.log").write_text("".join(
            f"[{job}] {ts} {what}\n" for ts, what in lines))

    def status(self, *args):
        # launchd does not hold the label, stated through the stub rather than
        # assumed of the machine (sd:1266): only the log can decide here.
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", *args],
            env={"PATH": self.fx.path(stub_bin(self.fx.tmp)),
                 "HOME": str(self.fx.home),
                 "SYSTEM_TOOLS_CONFIG": str(self.fx.config),
                 "CRON_TEST_HELD": "0",
                 "CRON_TEST_CALLS": str(self.calls)},
            capture_output=True, text=True, timeout=60)

    def test_status_asks_the_stub_launchctl(self):
        # sd:1266. The helper must not reach the machine's own launchctl.
        self.install()
        self.log([("2026-09-19T08:07:32-0600", "done")])
        self.status("demo")
        self.assertIn("launchctl print", self.calls.read_text())

    def test_last_run_failed_exits_1(self):
        self.install()
        self.log([("2026-09-19T05:30:05-0600", "starting (cwd: /tmp)"),
                  ("2026-09-19T08:07:32-0600", "FAILED rc=1")])
        self.assertEqual(self.status("demo").returncode, 1)

    def test_hand_run_after_the_failure_exits_0(self):
        # The phantom finding itself: launchd still says exit 1 here, and the
        # log says the job has since run to completion.
        self.install()
        self.log([("2026-09-19T05:30:05-0600", "starting (cwd: /tmp)"),
                  ("2026-09-19T08:07:32-0600", "FAILED rc=1"),
                  ("2026-09-19T11:32:05-0600", "starting (cwd: /tmp)"),
                  ("2026-09-19T11:50:36-0600", "done")])
        self.assertEqual(self.status("demo").returncode, 0)

    def test_an_installed_job_with_no_recorded_run_exits_1(self):
        # REGRESSION (the #486 Codex review, third round). 0 here retires
        # launchd's counter in local-health-check, so an installed job with
        # nothing behind it must not answer 0. A missing log and a launchd
        # exit of 1 used to leave both checks silent.
        self.install()
        self.assertEqual(self.status("demo").returncode, 1)

    def test_not_installed_here_exits_3(self):
        # Convention 6: nothing to check is not a failure.
        self.log([("2026-09-19T08:07:32-0600", "FAILED rc=1")])
        self.assertEqual(self.status("demo").returncode, 3)

    def test_help_declares_the_codes(self):
        out = subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "help"],
                             capture_output=True, text=True, timeout=60).stdout
        self.assertIn("local-health-check reads these codes", out)


    def test_health_check_defers_to_this_status_for_cron_labels(self):
        # The other half of sd:1201, and the half that produced the noise:
        # local-health-check's launchd sweep must not read `last exit code`
        # for a <prefix>.cron.* label whose log supersedes it. Read from the
        # file rather than from memory, so moving the guard does not quietly
        # un-fix this. The prefix is matched loosely: a literal prefix and a
        # "$LABEL_PREFIX" expansion are the same guard.
        sweep = (FOLDER.parent / "local-health-check" / "health-check.sh").read_text()
        found = CRON_LABEL_ARM.search(sweep)
        skip = found.start() if found else -1
        reads_counter = sweep.find("""sed -n 's/.*last exit code""")
        self.assertNotEqual(skip, -1, "the cron-label skip is gone from health-check.sh")
        self.assertLess(skip, reads_counter,
                        "health-check.sh reads launchd's counter before skipping cron labels")

    def test_health_check_skips_a_cron_label_only_on_a_clean_status(self):
        # REGRESSION (the #486 Codex review, second round). The skip was
        # unconditional, so a job that went quiet -- a run that died before
        # writing an outcome, or a definition deleted while its plist stayed
        # loaded -- lost launchd's evidence and gained nothing in its place.
        # The skip now runs `cron-jobs.sh status <job>` and takes a 0 as the
        # demonstration that the log supersedes the counter.
        sweep = (FOLDER.parent / "local-health-check" / "health-check.sh").read_text()
        # The case arm that ends its line: the redaction helper earlier in
        # the file also matches cron labels, on one line with its answer.
        arm = CRON_LABEL_ARM_EOL.search(sweep)
        self.assertIsNotNone(arm, "the cron-label case arm is gone from health-check.sh")
        guard = sweep[arm.start():][:400]
        self.assertIn("cron-jobs.sh", guard,
                      "the cron-label skip no longer consults the job's own status")
        self.assertRegex(guard, r"\$\{label#[^}]*\.cron\.\}",
                         "the skip does not pass the job name to that status")
        self.assertNotRegex(sweep, r"\.cron\.\*\) continue",
                            "the cron-label skip is unconditional again")


# The launchd lifetime the stubs report unless a test asks for another one,
# and the id of the jetsam coalition printed below it. Any two fixed values
# do; what the tests turn on is that the script reads the first and never the
# second, and that a change of the first is noticed.
LIFETIME = "85948"
JETSAM = "99999"
# The boot the lifetime above was counted in, and a second one to reboot into.
# The coalition id is deliberately the same across the two: a boot-local
# counter hands out low ids again after a reboot, so the pair is what tells
# the lifetimes apart and the tests have to be able to hold the id fixed.
BOOT = "1789568182"
LATER_BOOT = "1789999999"


class JobDirectoriesAndLabelsTest(unittest.TestCase):
    """CRON_JOBS_EXTRA_DIRS and SYSTEM_TOOLS_LABEL_PREFIX.

    PIN. Jobs live in <config>/cron-jobs/jobs and in colon-separated extra
    directories read from the environment or from <config>/cron-jobs/.env, and
    a job in an extra directory overrides a same-named one in the config dir. Labels are
    <prefix>.cron.<job>, with the prefix defaulting to local.system-tools.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.extra = self.fx.tmp / "private-jobs"
        self.extra.mkdir()
        self.extra2 = self.fx.tmp / "more jobs"
        self.extra2.mkdir()

    def extra_env(self):
        return {"CRON_JOBS_EXTRA_DIRS": f"{self.extra}:{self.extra2}"}

    def run_script(self, *args, env=None):
        base = {"PATH": self.fx.path(stub_bin(self.fx.tmp)),
                "HOME": str(self.fx.home),
                "SYSTEM_TOOLS_CONFIG": str(self.fx.config), "CRON_TEST_HELD": "0"}
        base.update(env or {})
        return subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), *args],
                              env=base, capture_output=True, text=True, timeout=60)

    def test_a_job_in_an_extra_dir_runs(self):
        (self.extra2 / "private.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        result = self.fx.exec_job("private", extra_env=self.extra_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[private]", (self.fx.folder / "logs" / "private.log").read_text())

    def test_without_the_extra_dirs_the_job_is_unknown(self):
        (self.extra / "private.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        result = self.fx.exec_job("private")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no such job 'private'", result.stdout + result.stderr)

    def test_an_extra_dir_overrides_a_shipped_job(self):
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="exit 7"\n')
        (self.extra / "demo.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        self.assertEqual(self.fx.exec_job("demo").returncode, 7)
        result = self.fx.exec_job("demo", extra_env=self.extra_env())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_list_names_every_directory_once(self):
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        (self.extra / "demo.job").write_text('JOB_SCHEDULE="0 4 * * *"\nJOB_COMMAND="true"\n')
        (self.extra2 / "private.job").write_text('JOB_SCHEDULE="0 5 * * *"\nJOB_COMMAND="true"\n')
        out = self.run_script("list", env=self.extra_env()).stdout
        rows = [line.split()[0] for line in out.splitlines()[1:]]
        self.assertEqual(rows, ["demo", "private"])
        self.assertIn("0 4 * * *", out, "the overriding definition is not the one listed")

    def test_env_file_supplies_the_extra_dirs(self):
        # launchd starts `exec` with only PATH and HOME, so the scheduled run
        # learns the extra directories from .env.
        (self.extra / "private.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        (self.fx.conf_dir / ".env").write_text(f'CRON_JOBS_EXTRA_DIRS="{self.extra}"\n')
        result = self.fx.exec_job("private")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_exported_value_wins_over_env_file(self):
        (self.extra / "demo.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="exit 7"\n')
        (self.extra2 / "demo.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        (self.fx.conf_dir / ".env").write_text(f'CRON_JOBS_EXTRA_DIRS="{self.extra}"\n')
        result = self.fx.exec_job("demo", extra_env={"CRON_JOBS_EXTRA_DIRS": str(self.extra2)})
        self.assertEqual(result.returncode, 0, result.stderr)

    def job_env(self, extra_env=None):
        """Run a JOB_COMMAND job that writes its environment; return it as a dict.

        launchctl is the stub on PATH, so nothing reaches the real launchd.
        """
        marker = self.fx.tmp / "job-env"
        self.fx.write_job("envdump", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="env -0 > \'{marker}\'"\n')
        env = {"PATH": self.fx.path(stub_bin(self.fx.tmp))}
        env.update(extra_env or {})
        result = self.fx.exec_job("envdump", extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = marker.read_bytes().decode()
        return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)

    def test_env_file_values_reach_the_job(self):
        # REGRESSION (sd:1943). .env was sourced without being exported, so a
        # job never saw SYSTEM_TOOLS_LABEL_PREFIX: the health check or runner
        # status it called looked up local.system-tools labels instead.
        (self.fx.conf_dir / ".env").write_text(
            'SYSTEM_TOOLS_LABEL_PREFIX="example.test"\nCRON_TEST_FROM_ENV_FILE="yes"\n')
        env = self.job_env()
        self.assertEqual(env.get("SYSTEM_TOOLS_LABEL_PREFIX"), "example.test")
        self.assertEqual(env.get("CRON_TEST_FROM_ENV_FILE"), "yes")

    def test_an_exported_prefix_wins_over_env_file_in_the_job(self):
        # PIN. The exported value wins for the script, and the job sees the
        # same value the script used.
        (self.fx.conf_dir / ".env").write_text('SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n')
        env = self.job_env({"SYSTEM_TOOLS_LABEL_PREFIX": "other.example.test"})
        self.assertEqual(env.get("SYSTEM_TOOLS_LABEL_PREFIX"), "other.example.test")

    def test_an_exported_prefix_wins_over_env_file_for_the_script(self):
        # PIN. The script's own labels use the exported prefix, not .env's.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        (self.fx.conf_dir / ".env").write_text('SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n')
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "other.example.test.cron.demo.plist").write_text("<plist/>\n")
        self.assertEqual(self.run_script("verify", "demo").stdout.split()[0], "missing")
        out = self.run_script("verify", "demo",
                              env={"SYSTEM_TOOLS_LABEL_PREFIX": "other.example.test"}).stdout
        self.assertEqual(out.split()[0], "STALE")

    def test_default_label_prefix(self):
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / f"{LABEL_PREFIX}.cron.demo.plist").write_text("<plist/>\n")
        self.assertEqual(self.run_script("verify", "demo").stdout.split()[0], "STALE")

    def test_label_prefix_from_the_environment(self):
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "example.test.cron.demo.plist").write_text("<plist/>\n")
        self.assertEqual(self.run_script("verify", "demo").stdout.split()[0], "missing")
        out = self.run_script("verify", "demo",
                              env={"SYSTEM_TOOLS_LABEL_PREFIX": "example.test"}).stdout
        self.assertEqual(out.split()[0], "STALE")

    def test_install_writes_the_prefixed_label(self):
        # launchctl is the stub here: bootstrap and enable succeed silently.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        env = {"SYSTEM_TOOLS_LABEL_PREFIX": "example.test"}
        result = self.run_script("install", "demo", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.fx.home / "Library" / "LaunchAgents" / "example.test.cron.demo.plist"
        self.assertIn("<string>example.test.cron.demo</string>", plist.read_text())
        self.assertEqual(self.run_script("verify", "demo", env=env).stdout.split()[0], "ok")

    def test_jobs_come_from_the_config_dir_only(self):
        # PIN. The repository ships no jobs: a .job beside the script is not
        # read, and the config directory's jobs folder is.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        (self.fx.folder / "jobs").mkdir()
        (self.fx.folder / "jobs" / "shipped.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        (self.fx.folder / "examples").mkdir()
        (self.fx.folder / "examples" / "sample.job").write_text('JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        out = self.run_script("list").stdout
        rows = [line.split()[0] for line in out.splitlines()[1:]]
        self.assertEqual(rows, ["demo"])

    def test_list_with_no_jobs_names_the_config_dir(self):
        result = self.run_script("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.fx.jobs), result.stderr)
        self.assertIn("examples/", result.stderr)

    def test_install_all_installs_every_job(self):
        # PIN. There is no profile filter: --all is every job in the job
        # directories, including the extra ones.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        (self.extra / "private.job").write_text('JOB_SCHEDULE="0 4 * * *"\nJOB_COMMAND="true"\n')
        env = {**self.extra_env(), "CRON_JOBS_PROFILE": "other"}
        result = self.run_script("install", "--all", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        agents = self.fx.home / "Library" / "LaunchAgents"
        self.assertEqual(sorted(p.name for p in agents.glob("*.plist")),
                         [f"{LABEL_PREFIX}.cron.demo.plist", f"{LABEL_PREFIX}.cron.private.plist"])
        verify = self.run_script("verify", "--all", env=env)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_the_examples_parse(self):
        # The shipped examples are documentation, not installed jobs; each
        # must still load as a job when copied into the config directory.
        examples = sorted((FOLDER / "examples").glob("*.job"))
        self.assertTrue(examples, "local-cron-jobs/examples holds no .job files")
        for example in examples:
            shutil.copy(example, self.fx.jobs / example.name)
        result = self.run_script("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line.split()[0] for line in result.stdout.splitlines()[1:]]
        self.assertEqual(sorted(rows), sorted(e.stem for e in examples))

    def test_help_names_the_configuration(self):
        text = self.run_script("help").stdout
        for name in ("CRON_JOBS_EXTRA_DIRS", "SYSTEM_TOOLS_LABEL_PREFIX", "<config>/cron-jobs/jobs"):
            self.assertIn(name, text)


class InstallBootstrapTest(unittest.TestCase):
    """sd:2574. `install` reports a bootstrap launchd refused.

    REGRESSION. On a job that runs often, `bootout` returned before launchd
    finished tearing the label down; `bootstrap` then printed `Bootstrap
    failed: 5: Input/output error`, and `install --all` printed `installed:`
    for the job anyway, with nothing loaded. `install` now waits for the label
    to leave launchd after `bootout`, retries a refused bootstrap once, and
    otherwise prints `failed:` and exits 1. launchctl is the stub on PATH.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.calls = self.fx.tmp / "launchctl-calls"
        self.fx.write_job("demo", 'JOB_SCHEDULE="*/10 * * * *"\nJOB_COMMAND="true"\n')

    def run_script(self, *args, env=None):
        base = {"PATH": self.fx.path(stub_bin(self.fx.tmp)),
                "HOME": str(self.fx.home),
                "SYSTEM_TOOLS_CONFIG": str(self.fx.config), "CRON_TEST_HELD": "0",
                "CRON_TEST_CALLS": str(self.calls),
                "CRON_TEST_BOOTSTRAP_COUNT": str(self.fx.tmp / "bootstrap-count")}
        base.update(env or {})
        return subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), *args],
                              env=base, capture_output=True, text=True, timeout=60)

    def bootstraps(self):
        return [line for line in self.calls.read_text().splitlines() if line.startswith("launchctl bootstrap")]

    def test_a_refused_bootstrap_is_retried_once(self):
        result = self.run_script("install", "demo", env={"CRON_TEST_BOOTSTRAP_FAILS": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("installed: demo", result.stdout)
        self.assertNotIn("failed:", result.stdout + result.stderr)
        self.assertEqual(len(self.bootstraps()), 2)

    def test_a_bootstrap_refused_twice_is_reported_failed(self):
        result = self.run_script("install", "demo", env={"CRON_TEST_BOOTSTRAP_FAILS": "99"})
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, out)
        self.assertIn("failed: demo (Bootstrap failed: 5: Input/output error)", out)
        self.assertNotIn("installed:", out)
        self.assertEqual(len(self.bootstraps()), 2)
        self.assertNotIn("launchctl enable", self.calls.read_text())

    def test_install_all_goes_on_and_names_the_failed_jobs(self):
        self.fx.write_job("other", 'JOB_SCHEDULE="0 * * * *"\nJOB_COMMAND="true"\n')
        result = self.run_script("install", "--all", env={"CRON_TEST_BOOTSTRAP_FAILS": "99"})
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, out)
        self.assertIn("failed: demo (", out)
        self.assertIn("failed: other (", out)
        self.assertNotIn("installed:", out)
        self.assertIn("install failed for: demo other", result.stderr)
        self.assertEqual(len(self.bootstraps()), 4)

    def test_bootstrap_waits_for_the_label_to_leave_launchd(self):
        # launchd still holds the label for a moment after `bootout` returns:
        # the stub keeps answering `print` until a file it removes a second
        # after `bootout` is gone. The old fixed half-second sleep did not wait.
        held = self.fx.tmp / "held"
        held.write_text("")
        # A folder of its own: run_script rewrites stub_bin's launchctl.
        stub = self.fx.tmp / "teardown-bin"
        stub.mkdir()
        launchctl = stub / "launchctl"
        text = (stub_bin(self.fx.tmp) / "launchctl").read_text().replace(
            'case "$1" in\n',
            'case "$1" in\n'
            f'  bootout) (sleep 1; rm -f \'{held}\') >/dev/null 2>&1 & exit 0 ;;\n'
            f'  print) [ -e \'{held}\' ] || exit 1 ;;\n', 1)
        launchctl.write_text(text)
        launchctl.chmod(launchctl.stat().st_mode | stat.S_IXUSR)
        result = self.run_script("install", "demo", env={"CRON_TEST_HELD": "1", "PATH": self.fx.path(stub)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls.read_text().splitlines()
        after = calls[calls.index(next(c for c in calls if c.startswith("launchctl bootout"))) + 1:]
        before_bootstrap = after[:after.index(self.bootstraps()[0])]
        self.assertGreaterEqual(sum(c.startswith("launchctl print") for c in before_bootstrap), 2, calls)
        self.assertFalse(held.exists())


class HostJobsTest(unittest.TestCase):
    """Per-machine job lists: <config>/cron-jobs/jobs/<host>/.

    PIN. The shared jobs folder holds every machine's jobs; jobs/<host>/ holds
    this machine's, where <host> is CRON_JOBS_HOST or `hostname -s`,
    lower-cased. A host job overrides a same-named shared one, and another
    host's folder is never read.
    """

    JOB = 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="{}"\n'

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.mine = self.fx.jobs / "mini"
        self.other = self.fx.jobs / "studio"

    def env(self, host="Mini"):
        env = {"PATH": self.fx.path(stub_bin(self.fx.tmp)),
               "HOME": str(self.fx.home),
               "SYSTEM_TOOLS_CONFIG": str(self.fx.config), "CRON_TEST_HELD": "0"}
        if host is not None:
            env["CRON_JOBS_HOST"] = host
        return env

    def run_script(self, *args, host="Mini"):
        return subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), *args],
                              env=self.env(host), capture_output=True, text=True, timeout=60)

    def rows(self, host="Mini"):
        result = self.run_script("list", host=host)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {line.split()[0]: line for line in result.stdout.splitlines()[1:]}

    def installed(self):
        agents = self.fx.home / "Library" / "LaunchAgents"
        return sorted(p.name for p in agents.glob("*.plist"))

    def test_a_host_job_overrides_the_shared_one(self):
        self.fx.write_job("demo", self.JOB.format("exit 7"))
        self.mine.mkdir()
        (self.mine / "demo.job").write_text(self.JOB.format("true"))
        result = self.fx.exec_job("demo", extra_env={"CRON_JOBS_HOST": "MINI"})
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.rows()
        self.assertEqual(list(rows), ["demo"])
        self.assertEqual(rows["demo"].split()[7], "jobs/mini")

    def test_another_hosts_folder_is_ignored(self):
        self.fx.write_job("shared", self.JOB.format("true"))
        self.mine.mkdir()
        (self.mine / "local.job").write_text(self.JOB.format("true"))
        self.other.mkdir()
        (self.other / "elsewhere.job").write_text(self.JOB.format("true"))
        (self.other / "shared.job").write_text(self.JOB.format("exit 7"))
        rows = self.rows()
        self.assertEqual(sorted(rows), ["local", "shared"])
        self.assertEqual(rows["shared"].split()[7], "jobs")
        self.assertEqual(rows["local"].split()[7], "jobs/mini")
        result = self.fx.exec_job("elsewhere", extra_env={"CRON_JOBS_HOST": "mini"})
        self.assertIn("no such job 'elsewhere'", result.stdout + result.stderr)
        self.assertEqual(self.fx.exec_job("shared", extra_env={"CRON_JOBS_HOST": "mini"}).returncode, 0)
        # The other machine sees its own folder and not this one's.
        self.assertEqual(sorted(self.rows(host="studio")), ["elsewhere", "shared"])

    def test_the_plist_names_the_config_root_so_exec_finds_the_job(self):
        """launchd gives a job only the environment its plist names (sd:2519).

        The fixture's root is not the default one under HOME, so a plist that
        names only PATH and HOME sends `exec` to an empty jobs folder.
        """
        self.fx.write_job("demo", self.JOB.format("true"))
        result = self.run_script("install", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.fx.home / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.cron.demo.plist"
        environment = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
        self.assertEqual(environment.get("SYSTEM_TOOLS_CONFIG"), str(self.fx.config))
        # The run launchd would start: the plist's environment and nothing
        # else, bar this fixture's quiet stubs in front of its PATH and a
        # report command that files nothing in the shared database.
        run = {**environment, "PATH": self.fx.path(), "SD_REPORT_BIN": "/usr/bin/true"}
        done = subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
                              env=run, capture_output=True, text=True, timeout=60)
        self.assertNotIn("no such job", done.stdout + done.stderr)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_plist_values_are_xml_escaped(self):
        """A config root named with `&`, `<` and `>` still renders a plist
        launchd can parse, holding the root as written (sd:2562)."""
        self.fx.write_job("demo", self.JOB.format("true"))
        odd = self.fx.tmp / "cfg & <odd>"
        odd.symlink_to(self.fx.config)
        env = {**self.env(), "SYSTEM_TOOLS_CONFIG": str(odd)}
        result = subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "install", "demo"],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.fx.home / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.cron.demo.plist"
        environment = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
        self.assertEqual(environment["SYSTEM_TOOLS_CONFIG"], str(odd))

    def test_no_host_folder_reads_the_shared_jobs(self):
        self.fx.write_job("demo", self.JOB.format("true"))
        self.assertEqual(list(self.rows()), ["demo"])
        self.assertEqual(self.rows()["demo"].split()[7], "jobs")
        # Without CRON_JOBS_HOST the machine's own name is used; the temp
        # config has no folder for it, so only the shared job is listed.
        self.assertEqual(list(self.rows(host=None)), ["demo"])

    def test_install_all_is_shared_plus_this_host(self):
        self.fx.write_job("shared", self.JOB.format("true"))
        self.mine.mkdir()
        (self.mine / "local.job").write_text(self.JOB.format("true"))
        self.other.mkdir()
        (self.other / "elsewhere.job").write_text(self.JOB.format("true"))
        result = self.run_script("install", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed(), [f"{LABEL_PREFIX}.cron.local.plist",
                                            f"{LABEL_PREFIX}.cron.shared.plist"])
        verify = self.run_script("verify", "--all")
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        # uninstall --all reaches this host's jobs too, as it did the shared.
        result = self.run_script("uninstall", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed(), [])

    def test_env_file_supplies_the_host(self):
        # launchd starts `exec` with only PATH and HOME; .env can pin the
        # host so a renamed machine keeps its folder.
        self.mine.mkdir()
        (self.mine / "local.job").write_text(self.JOB.format("true"))
        (self.fx.conf_dir / ".env").write_text('CRON_JOBS_HOST="mini"\n')
        self.assertEqual(self.fx.exec_job("local").returncode, 0)
        self.assertEqual(sorted(self.rows(host="studio")), [], "an exported host must win over .env")

    def test_help_names_the_host_folder(self):
        text = self.run_script("help").stdout
        for name in ("CRON_JOBS_HOST", "<config>/cron-jobs/jobs/<host>/*.job"):
            self.assertIn(name, text)


def stub_bin(tmp):
    """A directory to put in front of PATH, holding the two commands `status`
    asks the machine about. The stubs answer what the test sets: launchd's run
    counter, whether the label is running, the resource coalition id and the
    boot time, which together say which launchd lifetime the counter belongs
    to. The jetsam coalition is printed after the resource one with an id of
    its own, because that is the block a careless parse picks up instead.

    A label launchd has bootstrapped and not yet spawned prints `runs = 0`,
    `last exit code = (never exited)` and no coalition block at all, because
    no process has been placed in one. That was measured on this machine
    rather than assumed -- a probe agent bootstrapped and never kickstarted
    printed exactly that, and printed it again after `bootout` plus
    `bootstrap` reset the counter of a label that had already exited 7 -- and
    the stub reproduces it: a zero that still printed an id, or a stale exit
    code, would let a test pass against a script that reads either first.
    Everything else `launchctl` is asked succeeds silently, which is all
    `status` needs.
    """
    folder = tmp / "stub-bin"
    folder.mkdir(exist_ok=True)
    launchctl = folder / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        # Each call is logged when the test names a file, so a test can
        # assert that this stub, and not the machine's launchctl, answered.
        "[ -z \"${CRON_TEST_CALLS:-}\" ] || echo \"launchctl $*\" >> \"$CRON_TEST_CALLS\"\n"
        "case \"$1\" in\n"
        "  print)\n"
        # A label launchd does not hold: `launchctl print` fails and prints
        # nothing, so every field the script reads comes back empty. That is
        # a different state from a label it holds and has not spawned, and
        # the two must not be confused.
        "    [ \"${CRON_TEST_HELD:-1}\" = 1 ] || exit 1\n"
        "    printf '\\tstate = %s\\n' \"${CRON_TEST_STATE:-not running}\"\n"
        "    printf '\\truns = %s\\n' \"${CRON_TEST_RUNS:-1}\"\n"
        "    if [ \"${CRON_TEST_RUNS:-1}\" = 0 ]; then\n"
        "      printf '\\tlast exit code = (never exited)\\n'\n"
        "    else\n"
        "      printf '\\tlast exit code = %s\\n' \"${CRON_TEST_EXIT:-1}\"\n"
        "    fi\n"
        "    printf '\\tjob state = exited\\n'\n"
        "    if [ \"${CRON_TEST_RUNS:-1}\" != 0 ]; then\n"
        "      printf '\\tresource coalition = {\\n\\t\\tID = %s\\n\\t}\\n' "
        "\"${CRON_TEST_LIFETIME:-" + LIFETIME + "}\"\n"
        "      printf '\\tjetsam coalition = {\\n\\t\\tID = " + JETSAM + "\\n\\t}\\n'\n"
        "    fi\n"
        "    exit 0 ;;\n"
        # sd:2574. A bootstrap that launchd refuses: the first
        # CRON_TEST_BOOTSTRAP_FAILS calls exit 5 with launchd's own message.
        # Unset, bootstrap succeeds silently as before.
        "  bootstrap)\n"
        "    [ -n \"${CRON_TEST_BOOTSTRAP_FAILS:-}\" ] || exit 0\n"
        "    n=$(( $(cat \"$CRON_TEST_BOOTSTRAP_COUNT\" 2>/dev/null || echo 0) + 1 ))\n"
        "    echo \"$n\" > \"$CRON_TEST_BOOTSTRAP_COUNT\"\n"
        "    [ \"$n\" -gt \"$CRON_TEST_BOOTSTRAP_FAILS\" ] && exit 0\n"
        "    echo 'Bootstrap failed: 5: Input/output error' >&2\n"
        "    exit 5 ;;\n"
        "esac\n"
        "exit 0\n")
    launchctl.chmod(launchctl.stat().st_mode | stat.S_IXUSR)
    sysctl = folder / "sysctl"
    # `sysctl -n kern.boottime` prints the struct and the date beside it; the
    # script reads the `sec =` field out of exactly this shape, and `usec` on
    # the same line is what its pattern has to walk past.
    sysctl.write_text(
        "#!/bin/sh\n"
        "printf '{ sec = %s, usec = 661834 } Wed Sep 16 08:16:22 2026\\n' "
        "\"${CRON_TEST_BOOT:-" + BOOT + "}\"\n")
    sysctl.chmod(sysctl.stat().st_mode | stat.S_IXUSR)
    return folder


def stub_env(fx, folder, runs, lifetime=LIFETIME, state="not running",
             boot=BOOT, last_exit=1, held=True):
    """`last_exit` is launchd's own verdict on the last run it spawned, and it
    defaults to a failure because that is what every case below is about: what
    may, and may not, retire it."""
    return {"PATH": fx.path(folder),
            "HOME": str(fx.home),
            "SYSTEM_TOOLS_CONFIG": str(fx.config),
            # `true reports ingest ...` exits 0 and records nothing, so an
            # `exec` from here cannot file a run in the shared database.
            "SD_REPORT_BIN": "/usr/bin/true",
            "CRON_TEST_EXIT": str(last_exit),
            "CRON_TEST_HELD": "1" if held else "0",
            "CRON_TEST_RUNS": str(runs),
            "CRON_TEST_LIFETIME": str(lifetime),
            "CRON_TEST_BOOT": str(boot),
            "CRON_TEST_STATE": state}


def record_fields(fx, job="demo"):
    """The record beside the log, as the pairs the script writes.

    Four now: the run's own exit code, and the three fields that say which run
    it was.
    """
    text = (fx.folder / "logs" / f".{job}.runs").read_text()
    return dict(line.split("=", 1) for line in text.split() if "=" in line)


class StaleLogTest(unittest.TestCase):
    """A `done` supersedes launchd's counter only when it is the newest run.

    REGRESSION (the #486 Codex review, fourth round). The first three rounds
    closed every failure the script could see, and left one it could not: a
    scheduled run that dies before the script writes anything leaves the
    previous `done` reading as current, so `status` exits 0 and
    local-health-check drops the counter that did know.

    `launchctl print` gives no time for the exit it reports, so "demonstrably
    later" is not a clock comparison. It is a count. launchd increments `runs`
    when it spawns, measured rather than assumed -- a probe agent kickstarted
    three times read `runs = 1`, `2`, `3` from inside its own three runs -- so
    a counter ahead of the number the last outcome recorded is a run the log
    never saw.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        self.bin = stub_bin(self.fx.tmp)
        self.log([("2026-09-19T11:32:05-0600", "starting (cwd: /tmp)"),
                  ("2026-09-19T11:50:36-0600", "done")])

    def log(self, lines, job="demo"):
        (self.fx.folder / "logs" / f"{job}.log").write_text("".join(
            f"[{job}] {ts} {what}\n" for ts, what in lines))

    def recorded(self, value, job="demo", lifetime=LIFETIME, boot=BOOT,
                 exit_code=0):
        (self.fx.folder / "logs" / f".{job}.runs").write_text(
            f"exit={exit_code}\nruns={value}\nlifetime={lifetime}\nboot={boot}\n")

    def status(self, runs, *args, last_exit=1):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", "demo", *args],
            env=stub_env(self.fx, self.bin, runs, last_exit=last_exit),
            capture_output=True, text=True, timeout=60)

    def test_a_launchd_run_the_log_never_recorded_exits_1(self):
        """The finding itself: an older `done` must not retire a newer failure."""
        self.recorded(4)
        done = self.status(5)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("wrote nothing", done.stdout)

    def test_a_hand_rerun_after_a_scheduled_failure_still_exits_0(self):
        """sd:1201's own case, and it must keep working. A hand run never
        reaches launchd, so it records the counter the failed scheduled run
        already had: the numbers agree and the log supersedes."""
        self.recorded(4)
        done = self.status(4)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_a_job_with_no_recorded_counter_keeps_launchd_s_evidence(self):
        """REGRESSION (the #486 Codex review, fifth round). Absence cannot
        prove recovery.

        An installation that predates this carries an old `done` and no
        record, and a later startup failure cannot write one, so treating the
        gap as a grace would suppress that failure for good. The job is
        reported until its next completed run writes a number. `install`
        writes one too, so this is bounded to jobs that predate the change.
        """
        done = self.status(9)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("no run has recorded an outcome yet", done.stdout)

    def test_a_counter_that_restarted_does_not_read_as_accounted_for(self):
        """REGRESSION (the #486 Codex review, fifth round). `install` bootouts
        and bootstraps the label, and so does a reboot; launchd's count starts
        again while the record on disk does not. A recorded 4 would otherwise
        read a fresh run numbered 1 as covered, hiding every failure until the
        count climbed past 4. The test is equality, which answers both
        directions at once.
        """
        self.recorded(4)
        done = self.status(1)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("reloaded", done.stdout)

    def test_an_exec_writes_the_counter_it_saw(self):
        """The file is written by the run, from launchd's own count, or the
        comparison above has nothing to compare against."""
        subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, 7),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(record_fields(self.fx),
                         {"exit": "0", "runs": "7", "lifetime": LIFETIME,
                          "boot": BOOT})

    def test_the_lifetime_is_the_resource_coalition_and_not_the_jetsam_one(self):
        """`launchctl print` names two coalitions and only the first tracks the
        label's lifetime. A parse that fell through to the jetsam block would
        still compare two ids and still look like it worked, so the stub gives
        the two blocks different ids and this reads which one came back."""
        subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, 7),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(record_fields(self.fx)["lifetime"], LIFETIME)
        self.assertNotEqual(record_fields(self.fx)["lifetime"], JETSAM)


class EveryRunLeavesAnOutcomeTest(unittest.TestCase):
    """A run that dies before the end still records one, so `last_outcome`
    describes THIS run and not the one before it.

    REGRESSION (the #486 Codex review). `status` and local-health-check both
    read the log, and sd:1201 removed launchd's counter as the second opinion
    for <prefix>.cron.* labels. `cmd_exec` wrote "done" or "FAILED rc=N" only
    on the paths that reach the end of the function, and `set -e` has earlier
    exits: an unusable JOB_DIR terminated the shell at the `cd`, after the
    "starting" line and before any outcome. `last_outcome` then answered with
    the previous run's "done", `status` exited 0, and neither check saw the
    failure. The EXIT trap now writes the outcome for any run that started
    without one.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.calls = self.fx.tmp / "launchctl-calls"
        self.calls.touch()

    def log_text(self, job="demo"):
        return (self.fx.folder / "logs" / f"{job}.log").read_text()

    def install(self, job="demo"):
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"local.system-tools.cron.{job}.plist").write_text("<plist/>\n")

    def status(self, *args):
        # launchd does not hold the label, stated through the stub rather than
        # assumed of the machine (sd:1266): only the log can decide here.
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", *args],
            env={"PATH": self.fx.path(stub_bin(self.fx.tmp)),
                 "HOME": str(self.fx.home),
                 "SYSTEM_TOOLS_CONFIG": str(self.fx.config),
                 "CRON_TEST_HELD": "0",
                 "CRON_TEST_CALLS": str(self.calls)},
            capture_output=True, text=True, timeout=60)

    def test_status_asks_the_stub_launchctl(self):
        # sd:1266. The helper must not reach the machine's own launchctl.
        self.install()
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        self.status("demo")
        self.assertIn("launchctl print", self.calls.read_text())

    def test_an_unusable_job_dir_records_a_failure(self):
        missing = self.fx.tmp / "no-such-directory"
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\n'
                                  'JOB_COMMAND="true"\n'
                                  f'JOB_DIR="{missing}"\n')
        result = self.fx.exec_job("demo")
        self.assertNotEqual(result.returncode, 0)
        text = self.log_text()
        # The `cd` precedes the "starting" line, so this run reaches neither
        # that nor the end of the function. The trap is the only writer left.
        self.assertNotIn("starting", text)
        self.assertRegex(text, r"FAILED rc=[0-9]+")

    def test_the_failure_is_what_status_reads(self):
        # The whole point: the outcome has to reach the reader, not just the
        # log. A previous run that finished is on the log first, so a stale
        # answer would read "done" and exit 0.
        missing = self.fx.tmp / "no-such-directory"
        self.install()
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        self.assertEqual(self.fx.exec_job("demo").returncode, 0)
        self.assertEqual(self.status("demo").returncode, 0)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\n'
                                  'JOB_COMMAND="true"\n'
                                  f'JOB_DIR="{missing}"\n')
        self.fx.exec_job("demo")
        self.assertEqual(self.status("demo").returncode, 1)

    def test_a_run_that_ends_normally_records_one_outcome_only(self):
        # PIN. The trap must not add a second line to a run that already wrote
        # its own: a duplicate "FAILED" after "done" would invert the verdict.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        self.assertEqual(self.fx.exec_job("demo").returncode, 0)
        text = self.log_text()
        self.assertEqual(len(re.findall(r"\] [0-9T:+-]+ (?:done|FAILED rc=)", text)), 1)
        self.assertNotIn("FAILED", text)

    def test_a_deleted_definition_records_a_failure(self):
        # REGRESSION (the #486 Codex review, second round). A plist outlives
        # its `.job` file easily: delete the definition and launchd keeps
        # firing the label. `load_job` exited before the log was claimed, so
        # the run wrote nothing, `last_outcome` answered with the previous
        # run's "done", and `status` exited 0 for a job failing every slot.
        self.install()
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        self.assertEqual(self.fx.exec_job("demo").returncode, 0)
        self.assertIn("done", self.log_text())
        (self.fx.jobs / "demo.job").unlink()
        self.assertNotEqual(self.fx.exec_job("demo").returncode, 0)
        self.assertRegex(self.log_text(), r"FAILED rc=[0-9]+")
        self.assertEqual(self.status("demo").returncode, 1)

    def test_a_failing_command_still_records_one_outcome_only(self):
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="exit 7"\n')
        self.assertEqual(self.fx.exec_job("demo").returncode, 7)
        text = self.log_text()
        self.assertEqual(len(re.findall(r"\] [0-9T:+-]+ (?:done|FAILED rc=)", text)), 1)
        self.assertIn("FAILED rc=7", text)


class LaunchdLifetimeTest(unittest.TestCase):
    """The recorded count answers only for the launchd lifetime it came from.

    REGRESSION (the #486 review, sixth and seventh rounds). `runs` restarts at
    1 whenever the label is bootstrapped again, while the record beside the log
    persists, so a success recorded as run 1 and a later startup failure that
    logged nothing are both "run 1" and equality alone accepts the stale
    success. The sixth round paired the count with the machine's boot time,
    which separates a reboot and nothing else: `launchctl bootout` followed by
    `bootstrap` restarts the count inside one boot and the token does not move.
    The record carries the label's resource coalition id instead, which changes
    exactly where the count restarts.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        self.bin = stub_bin(self.fx.tmp)

    def exec_job(self, runs, lifetime=LIFETIME, boot=BOOT):
        """A run that writes its own record, rather than a record written by
        hand: the format is the script's business, and a test that writes one
        itself proves nothing about the format the script reads back."""
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, runs, lifetime, boot=boot),
            capture_output=True, text=True, timeout=60)

    def status(self, runs, lifetime=LIFETIME, boot=BOOT, last_exit=1):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", "demo"],
            env=stub_env(self.fx, self.bin, runs, lifetime, boot=boot,
                         last_exit=last_exit),
            capture_output=True, text=True, timeout=60)

    def test_a_reload_inside_one_boot_is_not_accounted_for(self):
        """The finding: one success survives the reload that reset the count.

        The run recorded is run 1 of the lifetime before `launchctl bootout`
        and `bootstrap`. The run launchd is reporting is run 1 of the lifetime
        after them -- a job that died at startup and wrote nothing. The numbers
        agree, the machine never rebooted, and the coalition the label's
        processes sit in is a different one.
        """
        self.assertEqual(self.exec_job(1).returncode, 0)
        done = self.status(1, lifetime="85944")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("reloaded", done.stdout)

    def test_the_verdict_is_the_first_line_of_stdout(self):
        """sd:1387 item 4. local-health-check quotes line 1 as the finding.

        It read "local-cron-jobs: === <job> ===" on a real night,
        a heading that names no fault. The verdict now leads, naming the job.
        """
        self.assertEqual(self.exec_job(1).returncode, 0)
        broken = self.status(1, lifetime="85944")
        self.assertEqual(broken.returncode, 1, broken.stdout)
        first = broken.stdout.splitlines()[0]
        self.assertTrue(first.startswith("local-cron-jobs: FAIL"), first)
        self.assertIn("demo", first)
        healthy = self.status(1)
        self.assertEqual(healthy.returncode, 0, healthy.stdout)
        first = healthy.stdout.splitlines()[0]
        self.assertTrue(first.startswith("local-cron-jobs: OK"), first)

    def test_a_run_in_the_same_lifetime_still_supersedes(self):
        # PIN. The token separates lifetimes; it must not report every job in
        # the one it is in.
        self.assertEqual(self.exec_job(1).returncode, 0)
        self.assertEqual(self.status(1).returncode, 0, self.status(1).stdout)

    def test_a_coalition_id_handed_out_again_after_a_reboot_is_not_a_match(self):
        """REGRESSION (the #486 review, seventh round). The coalition id is a
        boot-local counter, not a persistent identity.

        It starts near zero at every boot and climbs, so a label bootstrapped
        early after a reboot can be handed an id a record written before that
        reboot already names. Measured on the machine this was written on:
        daemons bootstrapped at boot carry ids 329 and 339 while a label
        loaded four days into the same uptime carries 16294. The recorded run
        is run 1 of a lifetime before the reboot; launchd is reporting run 1
        of a lifetime after it -- a job that died at startup and wrote
        nothing -- and the id is the same number both times. The boot token is
        what separates them, and without it `status` answered 0 while
        launchd's own exit said 1.
        """
        self.assertEqual(self.exec_job(1).returncode, 0)
        done = self.status(1, boot=LATER_BOOT)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("rebooted", done.stdout)
        # The mechanism behind that verdict: the run wrote the boot it ran in.
        self.assertEqual(record_fields(self.fx)["boot"], BOOT)

    def test_a_record_in_an_earlier_format_reads_as_no_record(self):
        """No earlier format carries an outcome, and none can name this
        lifetime. The bare number this began as has no keys at all. The
        `runs=`/`boot=` pair names a boot, which a reload does not change. The
        `runs=`/`lifetime=` pair names a coalition id, which repeats across
        boots. The three-field record names the run but not what it exited
        with. All four read as no record.

        MIGRATION. Installations on disk right now hold each of these, and
        reading one as no record is only half the answer: no record must mean
        no opinion. With launchd reporting a clean exit the job is not
        reported, and with launchd reporting a failure it is -- on launchd's
        evidence, not on the record's absence.

        A record carrying `exit=` and no identity is NOT one of these. It is
        the current format with fields absent, it carries an outcome, and the
        last case here says so by contrast.
        """
        (self.fx.folder / "logs" / "demo.log").write_text(
            "[demo] 2026-09-19T11:50:36-0600 done\n")
        for earlier in ("4\n",
                        "runs=4\nboot=1758000000\n",
                        f"runs=4\nlifetime={LIFETIME}\n",
                        f"runs=4\nlifetime={LIFETIME}\nboot={BOOT}\n"):
            with self.subTest(record=earlier):
                (self.fx.folder / "logs" / ".demo.runs").write_text(earlier)
                green = self.status(4, last_exit=0)
                self.assertEqual(green.returncode, 0, green.stdout)
                failed = self.status(4)
                self.assertEqual(failed.returncode, 1, failed.stdout)
                self.assertIn("no run has recorded an outcome yet", failed.stdout)
        # The contrast: `exit=` with no identity is this format, not an
        # earlier one, and it answers where the four above say nothing.
        (self.fx.folder / "logs" / ".demo.runs").write_text("exit=7\n")
        carried = self.status(4, last_exit=0)
        self.assertEqual(carried.returncode, 1, carried.stdout)
        self.assertIn("recorded exit 7", carried.stdout)


class ZeroRunLifetimeTest(unittest.TestCase):
    """A lifetime launchd has not spawned the job in yet reports nothing.

    REGRESSION (the #486 review, seventh round). A normal reboot bootstraps
    every label, and each one reads `runs = 0` with no coalition block until
    its next slot fires. The record beside the log still describes the
    lifetime before the reboot, so requiring a match called every previously
    green job broken -- nightly, for days for a weekly job and for weeks for a
    monthly one, with no failed invocation anywhere. Zero runs is no evidence,
    not evidence of an unlogged failure: launchd knows of no run the log could
    have missed, so the previous outcome stands until a new invocation
    supplies one.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * 0"\nJOB_COMMAND="true"\n')
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        self.bin = stub_bin(self.fx.tmp)

    def exec_job(self, runs, lifetime=LIFETIME, boot=BOOT):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, runs, lifetime, boot=boot),
            capture_output=True, text=True, timeout=60)

    def status(self, runs, lifetime=LIFETIME, boot=BOOT, last_exit=1):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", "demo"],
            env=stub_env(self.fx, self.bin, runs, lifetime, boot=boot,
                         last_exit=last_exit),
            capture_output=True, text=True, timeout=60)

    def test_a_reboot_does_not_report_a_job_that_was_green(self):
        """The finding: a weekly job finishes run 4, the machine reboots, and
        the next slot is six days away. launchd reports zero runs under a
        lifetime the record cannot describe, and that is not a failure."""
        self.assertEqual(self.exec_job(4).returncode, 0)
        self.assertEqual(self.status(4).returncode, 0)
        rebooted = self.status(0, lifetime="329", boot=LATER_BOOT)
        self.assertEqual(rebooted.returncode, 0, rebooted.stdout)
        self.assertNotIn("does not account", rebooted.stdout)

    def test_a_job_with_no_record_at_all_is_not_reported_either(self):
        """The same rule where there is nothing on disk. A `done` from a hand
        run and a launchd lifetime that has spawned nothing contradict each
        other in no way, so the counter retires no evidence here."""
        (self.fx.folder / "logs" / "demo.log").write_text(
            "[demo] 2026-09-19T11:50:36-0600 done\n")
        fresh = self.status(0, lifetime="329", boot=LATER_BOOT)
        self.assertEqual(fresh.returncode, 0, fresh.stdout)

    def test_the_first_run_of_the_new_lifetime_is_compared_again(self):
        """PIN. The grace lasts exactly as long as launchd has counted
        nothing. Once the new lifetime spawns run 1 and that run writes no
        outcome, the record still names the lifetime before the reboot and the
        job is reported."""
        self.assertEqual(self.exec_job(4).returncode, 0)
        broken = self.status(1, lifetime="329", boot=LATER_BOOT)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("rebooted", broken.stdout)


class RunInProgressTest(unittest.TestCase):
    """A run that is happening is not compared; a run that died is not covered.

    REGRESSION (the #486 review, sixth and seventh rounds). launchd increments
    `runs` when it spawns, so every status read from inside a scheduled run --
    which is what local-health-check is, run as a cron job of its own -- saw
    the counter one ahead of the last record and called the job broken for as
    long as the job took. The sixth round answered that by recording the count
    at spawn, which made a start indistinguishable from a completion: a run
    SIGKILLed after it started left the counter level beside the previous
    `done` and read as healthy. The record is written on completion only, and
    launchd's own `state` is what excuses a run in progress.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        self.bin = stub_bin(self.fx.tmp)

    def exec_job(self, runs, state="running"):
        """`exec` is what the plist invokes, so launchd holds the label as
        running for exactly as long as this call takes."""
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, runs, state=state),
            capture_output=True, text=True, timeout=60)

    def status(self, runs, state="not running"):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", "demo"],
            env=stub_env(self.fx, self.bin, runs, state=state),
            capture_output=True, text=True, timeout=60)

    def plain_job(self):
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')

    def test_a_running_job_is_not_called_broken(self):
        """The counter is ahead of the record because launchd counted the run
        in progress, and that run owes no outcome yet. Run 4 logged its `done`
        and launchd has spawned run 5."""
        self.plain_job()
        self.assertEqual(self.exec_job(4).returncode, 0)
        done = self.status(5, state="running")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("a run is in progress", done.stdout)

    def test_a_status_read_from_inside_a_scheduled_run_is_not_broken(self):
        """The same thing from where it hurt: the job asks `status` about
        itself while launchd runs it, which is local-health-check's own sweep.
        The probe writes to a file, so it does not append to the log it reads.
        """
        probe_out = self.fx.tmp / "probe.out"
        probe_rc = self.fx.tmp / "probe.rc"
        self.plain_job()
        self.assertEqual(self.exec_job(4).returncode, 0)
        # Single quotes so `$?` reaches bash instead of expanding when the
        # definition is sourced.
        self.fx.write_job("demo",
                          'JOB_SCHEDULE="0 3 * * *"\n'
                          "JOB_COMMAND='sh {script} status demo > {out} 2>&1;"
                          " echo $? > {rc}'\n".format(
                              script=self.fx.folder / "cron-jobs.sh",
                              out=probe_out, rc=probe_rc))
        self.assertEqual(self.exec_job(5).returncode, 0)
        self.assertEqual(probe_rc.read_text().strip(), "0", probe_out.read_text())

    def test_a_run_killed_after_its_start_leaves_the_counter_ahead(self):
        """The seventh round's finding. A start recorded as if it were an
        outcome made this run indistinguishable from one that finished: the
        reviewer's probe exited -9 and `status` answered 0 beside a stale
        `done`. Nothing is recorded at spawn now, so the run launchd counted
        has no record and the job is reported.

        stdout is the job's own log, the way the plist points it, so the run
        starts no tee and the SIGKILL leaves no process behind.
        """
        self.plain_job()
        self.assertEqual(self.exec_job(4).returncode, 0)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\n'
                                  f"JOB_COMMAND='{KILL_THE_RUNNER}'\n")
        log = self.fx.folder / "logs" / "demo.log"
        with open(log, "ab") as out:
            killed = subprocess.run(
                ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
                env=stub_env(self.fx, self.bin, 5, state="running"),
                stdout=out, stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        done = self.status(5)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("wrote nothing", done.stdout)
        # The mechanism behind that verdict: the EXIT trap does not run on
        # SIGKILL, so run 5 left the record naming run 4.
        self.assertEqual(record_fields(self.fx),
                         {"exit": "0", "runs": "4", "lifetime": LIFETIME,
                          "boot": BOOT})


class RecordedOutcomeTest(unittest.TestCase):
    """The outcome is read from the record the run wrote, and launchd's
    failure stands until a record retires it.

    REGRESSION (the #486 review, ninth round). Two findings, one inversion.

    F3: completion metadata and the parsed log outcome were two independent
    readings of one run, and they disagree when the job's own output has no
    trailing newline. `printf error; exit 7` writes `error[demo] ... FAILED
    rc=7`, which the anchored pattern does not match, while the identity
    fields advanced as usual -- so an earlier `done` passed every freshness
    check and suppressed the failure. The run now records its own exit code
    and nothing parses log text to decide pass or fail.

    F4: an installation that predates the record has a green log and no
    record, and requiring one reported it broken every night although
    launchd's own last exit was zero. Missing recovery evidence is not
    failure evidence. No record means no opinion, and launchd's verdict
    decides -- which reports the same job when launchd's exit is non-zero.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        self.bin = stub_bin(self.fx.tmp)
        self.log = self.fx.folder / "logs" / "demo.log"

    def job(self, command):
        self.fx.write_job("demo", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND={command}\n')

    def exec_job(self, runs):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, runs),
            capture_output=True, text=True, timeout=60)

    def status(self, runs, last_exit):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", "demo"],
            env=stub_env(self.fx, self.bin, runs, last_exit=last_exit),
            capture_output=True, text=True, timeout=60)

    def parsed_latest_outcome(self):
        """What an anchored parse of the log text answers, which is what the
        decision used to rest on."""
        found = re.findall(r"^\[demo\] [0-9T:+-]+ (done|FAILED rc=[0-9]+)",
                           self.log.read_text(), re.M)
        return found[-1] if found else ""

    def test_a_failure_whose_output_lacks_a_newline_is_still_reported(self):
        """F3, driven the way the reviewer drove it. Run 1 succeeds, run 2
        exits 7 after writing output with no trailing newline, and launchd is
        made to report a clean exit so that only the record can answer."""
        self.job('"true"')
        self.assertEqual(self.exec_job(1).returncode, 0)
        self.job("'printf error; exit 7'")
        self.assertEqual(self.exec_job(2).returncode, 7)

        # The disagreement itself: the failure is in the log, and an anchored
        # parse of the log still answers with run 1's `done`.
        self.assertIn("error[demo]", self.log.read_text())
        self.assertEqual(self.parsed_latest_outcome(), "done")

        # The verdict does not rest on that parse any more.
        broken = self.status(2, last_exit=0)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("recorded exit 7", broken.stdout)
        # The mechanism behind it: run 2 wrote its own exit code down.
        self.assertEqual(record_fields(self.fx)["exit"], "7")

    def test_the_record_outranks_the_log_text(self):
        """The same rule with the disagreement made blatant. Whatever the log
        says, the run that finished is the one that knows what it exited
        with."""
        self.job("'exit 7'")
        self.assertEqual(self.exec_job(2).returncode, 7)
        self.log.write_text("[demo] 2026-09-21T03:00:00-0600 done\n")
        self.assertEqual(self.parsed_latest_outcome(), "done")
        broken = self.status(2, last_exit=0)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("recorded exit 7", broken.stdout)

    def test_a_green_job_with_no_record_is_not_reported(self):
        """F4, the migration case. Every installation that predates the record
        looks like this: a log full of successful runs, nothing beside it, and
        launchd reporting a clean exit. Nothing here is evidence of a failure.
        """
        self.job('"true"')
        self.log.write_text("[demo] 2026-09-20T03:00:12-0600 done\n")
        self.assertFalse((self.fx.folder / "logs" / ".demo.runs").exists())
        green = self.status(3, last_exit=0)
        self.assertEqual(green.returncode, 0, green.stdout)

    def test_a_missing_record_does_not_suppress_a_launchd_failure(self):
        """The inverse, and the half F4 must not take with it. The same job
        with the same missing record is reported when launchd's own last exit
        is non-zero: absence of evidence suppresses nothing either."""
        self.job('"true"')
        self.log.write_text("[demo] 2026-09-20T03:00:12-0600 done\n")
        broken = self.status(3, last_exit=1)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("exited 1", broken.stdout)

    def test_a_manual_run_records_its_failure_with_no_launchd_identity(self):
        """REGRESSION (the #486 review, tenth round). Every completed run has
        an outcome; only some have a launchd identity.

        launchd holds no coalition for a label it has bootstrapped and not yet
        spawned, which is every label after a reboot, so a run by hand in that
        window could not name a lifetime -- and the writer aborted on that,
        leaving the previous lifetime's record to answer for it. The probe:
        a scheduled success, a reboot, a manual run that exits 7, and `status`
        answering 0 beside it.
        """
        self.job('"true"')
        self.assertEqual(self.exec_job(4).returncode, 0)
        self.assertEqual(record_fields(self.fx)["exit"], "0")
        # The reboot: bootstrapped and not yet spawned, so `runs = 0`, no
        # coalition block and `last exit code = (never exited)`.
        self.job("'exit 7'")
        self.assertEqual(self.exec_job(0).returncode, 7)
        broken = self.status(0, last_exit=0)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("recorded exit 7", broken.stdout)
        # The mechanism: `exit=` is written, and the identity is left out
        # rather than aborting the write.
        self.assertEqual(record_fields(self.fx), {"exit": "7"})

    def test_a_manual_run_clears_a_failure_with_no_launchd_identity(self):
        """REGRESSION (the #486 review, tenth round), the other direction and
        the one that costs an operator time: a hand-run recovery could not
        clear a finding until the next scheduled invocation, which for a
        weekly job is days. The probe read `exec_exit=0 status_exit=1`.
        """
        self.job("'exit 7'")
        self.assertEqual(self.exec_job(4).returncode, 7)
        self.assertEqual(self.status(4, last_exit=7).returncode, 1)
        self.job('"true"')
        self.assertEqual(self.exec_job(0).returncode, 0)
        fixed = self.status(0, last_exit=7)
        self.assertEqual(fixed.returncode, 0, fixed.stdout)
        self.assertEqual(record_fields(self.fx), {"exit": "0"})

    def test_a_record_without_identity_waits_behind_launchd_to_suppress(self):
        """A record with no identity carries an outcome and cannot supersede.

        The two halves of one rule. A recorded FAILURE reports on its own:
        nothing has superseded it, and reporting is the safe direction. A
        recorded SUCCESS may not retire launchd's failure, because "later than
        launchd's last spawn" is what the three identity fields prove and
        nothing else does -- so it waits behind launchd's verdict and answers
        only where launchd has none.
        """
        self.job('"true"')
        record = self.fx.folder / "logs" / ".demo.runs"
        record.write_text("exit=0\n")
        # launchd reports a failure: the success cannot prove it came after.
        self.assertEqual(self.status(5, last_exit=1).returncode, 1)
        # launchd reports a clean exit: nothing to contradict.
        self.assertEqual(self.status(5, last_exit=0).returncode, 0)
        # A failure recorded the same way reports even against a clean launchd.
        record.write_text("exit=7\n")
        broken = self.status(5, last_exit=0)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("recorded exit 7", broken.stdout)

    def test_a_hand_run_after_a_scheduled_failure_retires_it(self):
        """The one thing that may retire launchd's failure, end to end:
        sd:1201's own case. launchd spawned run 2 and reports exit 7; a hand
        run afterwards never reaches launchd, so it records launchd's current
        count with its own exit 0, and that is a later completed success in
        this lifetime."""
        self.job("'exit 7'")
        self.assertEqual(self.exec_job(2).returncode, 7)
        self.assertEqual(self.status(2, last_exit=7).returncode, 1)
        self.job('"true"')
        self.assertEqual(self.exec_job(2).returncode, 0)
        self.assertEqual(record_fields(self.fx)["exit"], "0")
        fixed = self.status(2, last_exit=7)
        self.assertEqual(fixed.returncode, 0, fixed.stdout)

    def test_a_hand_run_replaces_a_record_it_cannot_write_in_place(self):
        """REGRESSION (sd:1265, the codex probe on #493). The record is
        replaced by rename, so a read-only record in a writable folder is
        still replaced: a failed hand run after a recorded success reports,
        where an in-place write failed silently and left the success."""
        self.job('"true"')
        self.assertEqual(self.exec_job(1).returncode, 0)
        record = self.fx.folder / "logs" / ".demo.runs"
        record.chmod(0o444)
        self.job("'exit 7'")
        self.assertEqual(self.exec_job(1).returncode, 7)
        self.assertEqual(record_fields(self.fx)["exit"], "7")
        broken = self.status(1, last_exit=7)
        self.assertEqual(broken.returncode, 1, broken.stdout)

    def test_a_record_that_cannot_be_written_keeps_the_attempt(self):
        """sd:1265. When no record can be written at all, the attempt marker
        stays, so the earlier success is withdrawn rather than left to
        answer for a run that failed."""
        self.job('"true"')
        self.assertEqual(self.exec_job(1).returncode, 0)
        logs = self.fx.folder / "logs"
        (logs / ".demo.runs").chmod(0o444)
        # A directory where the temp file goes: its write fails too.
        (logs / ".demo.runs.tmp").mkdir()
        self.job("'exit 7'")
        self.assertEqual(self.exec_job(1).returncode, 7)
        self.assertEqual(record_fields(self.fx)["exit"], "0")
        self.assertTrue((logs / ".demo.attempt").exists())
        broken = self.status(1, last_exit=7)
        self.assertEqual(broken.returncode, 1, broken.stdout)


class NeverSpawnedTest(unittest.TestCase):
    """A job launchd has not run yet is pending, not broken.

    REGRESSION (the #486 review, eleventh round). A newly installed job has no
    record, no log and no launchd verdict, and that read as unknown -- which
    this file reports, because a 0 with nothing behind it retires launchd's
    counter in local-health-check (round 3). So every newly installed job was
    a nightly finding until its first slot, which for a monthly job is weeks,
    with nothing anyone could act on.

    launchd distinguishes the two states itself. A label it holds and has not
    spawned prints `runs = 0` and `last exit code = (never exited)`; a label
    it does not hold prints neither. The first is launchd saying it has no
    verdict yet, and there is no outcome BECAUSE there has been no run. The
    second is round 3's case and stays reported.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        # A monthly job: the cadence that made this expensive.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 1 * *"\nJOB_COMMAND="true"\n')
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        self.bin = stub_bin(self.fx.tmp)

    def status(self, runs, last_exit=1, held=True):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", "demo"],
            env=stub_env(self.fx, self.bin, runs, last_exit=last_exit, held=held),
            capture_output=True, text=True, timeout=60)

    def test_a_newly_installed_job_is_not_reported_before_its_first_run(self):
        """The finding itself. Nothing on disk, nothing in the log, and
        launchd reporting `runs = 0` with `(never exited)`."""
        self.assertFalse((self.fx.folder / "logs" / "demo.log").exists())
        self.assertFalse((self.fx.folder / "logs" / ".demo.runs").exists())
        pending = self.status(0)
        self.assertEqual(pending.returncode, 0, pending.stdout)
        self.assertIn("not spawned yet", pending.stdout)

    def test_a_recorded_failure_survives_the_never_spawned_state(self):
        """The adjacent case, and the one a careless fix swallows. A run by
        hand after a reboot records `exit=` with no identity, because launchd
        holds no coalition for a label it has not spawned. That failure is
        still the last completed run there was, and pending is about the
        ABSENCE of an outcome, not about overriding one."""
        (self.fx.folder / "logs" / ".demo.runs").write_text("exit=7\n")
        broken = self.status(0)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("recorded exit 7", broken.stdout)

    def test_a_spawned_job_that_exited_non_zero_is_still_reported(self):
        """The other adjacent case: launchd has a verdict and it is a failure.
        Nothing about the never-spawned state may reach this."""
        broken = self.status(3, last_exit=1)
        self.assertEqual(broken.returncode, 1, broken.stdout)
        self.assertIn("exited 1", broken.stdout)

    def test_a_label_launchd_does_not_hold_is_still_reported(self):
        """PIN (round 3). `launchctl print` failing is not launchd saying it
        has not run the job; it is launchd saying nothing at all, and an
        installed plist that is not loaded must not go quiet. Both readings of
        the never-spawned state are required for exactly this reason."""
        unknown = self.status(0, held=False)
        self.assertEqual(unknown.returncode, 1, unknown.stdout)
        self.assertIn("holds no verdict", unknown.stdout)



class FailureNotificationTest(unittest.TestCase):
    """A staged failure notifies the fixture's stub, not the user's desktop.

    The suite posted 9 real macOS notifications per run at the user, measured
    with a recording `osascript` first on PATH. `notify_failure` calls bare
    `osascript`, and the fixture's PATH reached `/usr/bin/osascript`.

    Both PATHs a run can be started with are asserted here. The obvious fix --
    an `osascript` beside `launchctl` in `stub_bin` -- covers only the five
    that came through `stub_env`; the other four came through the plain PATH
    `Fixture.exec_job` builds, which never consults `stub_bin`. The stub is on
    the fixture and both builders go through `Fixture.path`.

    PIN, and a rig fix: `cron-jobs.sh` is unchanged, so these pass against
    earlier code too. A stub nothing asserts on goes stale silently.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="exit 3"\n')

    def test_a_failure_run_plainly_notifies_the_stub(self):
        self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        posted = self.fx.notifications()
        self.assertEqual(len(posted), 1, posted)
        self.assertIn("Job 'demo' failed (rc=3)", posted[0])

    def test_a_failure_run_under_the_launchctl_stubs_notifies_the_stub(self):
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        run = subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, stub_bin(self.fx.tmp), 2),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 3, run.stderr)
        posted = self.fx.notifications()
        self.assertEqual(len(posted), 1, posted)
        self.assertIn("Job 'demo' failed (rc=3)", posted[0])



class UnfinishedRunTest(unittest.TestCase):
    """A run that started and recorded nothing withdraws the record's success.

    REGRESSION (the #486 review, fifteenth round). launchd's identity cannot
    see a hand run: `run` and `exec` never reach launchd, so `runs` does not
    move and two successive hand runs carry the identical identity triple.
    The first one's `exit=0` therefore kept proving it was "the run launchd
    last spawned" after a second hand run died on SIGKILL without writing
    anything, and launchd's own failure stayed suppressed by a run that was no
    longer the newest. An attempt marker, written before the command runs and
    removed when an outcome is recorded, is the only evidence that the second
    run existed.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        self.bin = stub_bin(self.fx.tmp)
        self.log = self.fx.folder / "logs" / "demo.log"
        self.attempt = self.fx.folder / "logs" / ".demo.attempt"

    def job(self, command):
        self.fx.write_job("demo", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND={command}\n')

    def exec_job(self, command, runs=4):
        self.job(command)
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, runs),
            capture_output=True, text=True, timeout=60)

    def kill_a_run(self, runs=4):
        """A hand run that dies where no trap can see it. stdout is the job's
        own log, the way the plist points it, so the run starts no tee and the
        SIGKILL leaves no process behind."""
        self.job(f"'{KILL_THE_RUNNER}'")
        with open(self.log, "ab") as out:
            return subprocess.run(
                ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
                env=stub_env(self.fx, self.bin, runs),
                stdout=out, stderr=subprocess.PIPE, text=True, timeout=60)

    def status(self, runs=4, last_exit=1):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "status", "demo"],
            env=stub_env(self.fx, self.bin, runs, last_exit=last_exit),
            capture_output=True, text=True, timeout=60)

    def test_a_hand_run_killed_after_a_success_stops_suppressing_the_failure(self):
        """The finding itself. launchd's last scheduled run exited 1, a hand
        run then succeeded and retired it, and a second hand run was killed.
        The counter never moved, so the record still names "the run launchd
        last spawned" -- and it is no longer this job's newest run."""
        self.assertEqual(self.exec_job('"exit 7"').returncode, 7)
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        self.assertEqual(self.status().returncode, 0, "the hand run must retire the failure")
        killed = self.kill_a_run()
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        after = self.status()
        self.assertEqual(after.returncode, 1, after.stdout)
        # The mechanism: the record is untouched, and the marker is what says
        # a later run started.
        self.assertEqual(record_fields(self.fx),
                         {"exit": "0", "runs": "4", "lifetime": LIFETIME,
                          "boot": BOOT})
        self.assertEqual(self.attempt.read_text().strip(), BOOT)

    def test_a_dangling_attempt_is_not_a_failure_on_its_own(self):
        """Unknown is not broken. With launchd reporting a clean exit there is
        no failure anywhere, and a run that died without writing must not
        invent one."""
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        self.assertEqual(self.kill_a_run().returncode, -signal.SIGKILL)
        done = self.status(last_exit=0)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_a_completed_run_leaves_no_attempt_behind(self):
        """Both completions clear it: the success path in `cmd_exec` and the
        EXIT trap that catches a failure."""
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        self.assertFalse(self.attempt.exists(), "a completed success owes nothing")
        self.assertEqual(self.exec_job('"exit 7"').returncode, 7)
        self.assertFalse(self.attempt.exists(), "a caught failure owes nothing")

    def test_a_marker_from_an_earlier_boot_is_debris(self):
        """A machine that lost power mid-run leaves a marker no process will
        ever remove. The boot it was written in is in the file, and a marker
        from an earlier boot holds nothing back."""
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        self.assertEqual(self.kill_a_run().returncode, -signal.SIGKILL)
        self.attempt.write_text(f"{LATER_BOOT}\n")
        done = self.status()
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_a_hand_run_that_retires_a_failure_still_retires_it(self):
        """PIN, not a regression: this passes against the committed head too.
        It is here because the simpler alternative to this fix -- stop letting
        any record suppress launchd's failure -- would break exactly this, and
        this is sd:1201's whole complaint."""
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        done = self.status()
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertFalse(self.attempt.exists())


UTC_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class RunStampTest(UnfinishedRunTest.__base__):
    """Each run stamps its start, end and exit code beside its log (sd:2210).

    launchd keeps no run time, and a log's write time is not one: Management
    reads `logs/.<job>.stamp` as the job's last run. The start is written when
    the run takes its lock, the end and exit when it records an outcome, so a
    stamp with a start and no end is a run in progress or one no trap saw end.
    """

    setUp = UnfinishedRunTest.setUp
    job = UnfinishedRunTest.job
    exec_job = UnfinishedRunTest.exec_job
    kill_a_run = UnfinishedRunTest.kill_a_run

    def stamp(self):
        text = (self.fx.folder / "logs" / ".demo.stamp").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def test_a_finished_run_stamps_its_start_end_and_exit(self):
        self.assertEqual(self.exec_job('"exit 7"').returncode, 7)
        failed = self.stamp()
        self.assertEqual(sorted(failed), ["ended", "exit", "started"])
        self.assertRegex(failed["started"], UTC_STAMP)
        self.assertRegex(failed["ended"], UTC_STAMP)
        self.assertLessEqual(failed["started"], failed["ended"])
        self.assertEqual(failed["exit"], "7")
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        passed = self.stamp()
        self.assertEqual(passed["exit"], "0")
        self.assertGreaterEqual(passed["started"], failed["started"])

    def test_a_run_that_fails_before_its_command_still_stamps_its_end(self):
        """The EXIT trap's path: an unusable JOB_DIR ends the shell at the `cd`."""
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n'
                                  f'JOB_DIR="{self.fx.tmp}/no-such-dir"\n')
        done = subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
                              env=stub_env(self.fx, self.bin, 4), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(self.stamp()["exit"], "1")
        self.assertRegex(self.stamp()["ended"], UTC_STAMP)

    def test_a_run_no_trap_saw_end_leaves_a_start_and_no_end(self):
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        self.assertEqual(self.kill_a_run().returncode, -signal.SIGKILL)
        killed = self.stamp()
        self.assertEqual(sorted(killed), ["started"])
        self.assertRegex(killed["started"], UTC_STAMP)


def kill_group(pgid):
    """SIGKILL a process group that may already be gone."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def started(log, before=0):
    """Wait until `log` holds more `starting` lines than `before`: the run
    writes that line after it holds the lock."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if log.exists() and log.read_text().count(" starting (cwd:") > before:
            return True
        time.sleep(0.05)
    return False


class StaleLockTest(UnfinishedRunTest.__base__):
    """A killed run does not stop the next one, and a live job keeps its lock (sd:1251).

    REGRESSION. The lock was a directory the EXIT trap removed, and SIGKILL
    runs no trap: every later run printed "skipped: previous run still
    active" and exited 0 for good. Its first repair judged the owner's pid
    dead and removed the directory, which two runs could both do, and which
    a workload outliving its runner did not survive. The lock is now a
    kernel `flock` the workload inherits.
    """

    setUp = UnfinishedRunTest.setUp
    job = UnfinishedRunTest.job
    exec_job = UnfinishedRunTest.exec_job
    kill_a_run = UnfinishedRunTest.kill_a_run

    def run_in_background(self, command, **popen):
        self.job(command)
        before = self.log.read_text().count(" starting (cwd:") if self.log.exists() else 0
        run = subprocess.Popen(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, 4),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen)
        self.addCleanup(run.wait)
        self.assertTrue(started(self.log, before), "the run starts")
        return run

    def second_run(self):
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, 4),
            capture_output=True, text=True, timeout=60)

    def test_a_run_after_a_killed_run_is_not_skipped(self):
        self.assertEqual(self.kill_a_run().returncode, -signal.SIGKILL)
        after = self.exec_job('"true"')
        self.assertNotIn("skipped", after.stderr)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertTrue(self.log.read_text().rstrip().endswith("done"),
                        self.log.read_text()[-400:])

    def test_a_live_run_still_holds_the_lock(self):
        """PIN: the lock still does its job while its owner runs."""
        run = self.run_in_background('"sleep 30"', start_new_session=True)
        # The whole group: `sleep` outlives a kill of its shell alone.
        self.addCleanup(kill_group, run.pid)
        second = self.second_run()
        self.assertIn("skipped: previous run still active", second.stderr)
        self.assertEqual(second.returncode, 0)

    def test_a_job_that_outlives_its_runner_still_holds_the_lock(self):
        """REGRESSION (sd:1251 review). `bash -c` outlives a runner killed
        alone, and the runner's death is not the job's. The run shares this
        test's process group on purpose: group identity proves nothing, the
        held file does."""
        run = self.run_in_background('"exec sleep 31.4159"')
        self.addCleanup(subprocess.run, ["pkill", "-f", "sleep 31.4159"])
        os.kill(run.pid, signal.SIGKILL)
        run.wait()
        second = self.second_run()
        self.assertIn("skipped: previous run still active", second.stderr)

    def test_a_surviving_tee_does_not_hold_the_lock(self):
        """REGRESSION (#576 review). The log's tee is a helper, not the job.
        The workload here closes its own copy of the lock and keeps writing,
        so after the runner is killed only tee could still hold it."""
        run = self.run_in_background("'exec 8>&-; exec sleep 32.7182'")
        self.addCleanup(subprocess.run, ["pkill", "-f", "sleep 32.7182"])
        os.kill(run.pid, signal.SIGKILL)
        run.wait()
        after = self.exec_job('"true"')
        self.assertNotIn("skipped", after.stderr)
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_concurrent_runs_start_exactly_one(self):
        """REGRESSION (sd:1251 review). Six runs at once, after a killed one,
        and one starts: taking the lock is a single kernel call."""
        self.assertEqual(self.kill_a_run().returncode, -signal.SIGKILL)
        self.job('"sleep 2"')
        runs = [subprocess.Popen(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, 4),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            start_new_session=True) for _ in range(6)]
        errors = [run.communicate(timeout=60)[1] for run in runs]
        self.assertEqual(len([error for error in errors if "skipped" not in error]), 1, errors)

class AbandonedRunTest(UnfinishedRunTest.__base__):
    """A run that died unseen is reported, as its own state (sd:1259).

    REGRESSION. With launchd's last scheduled exit at 0, the ladder in
    `job_is_broken` ends at launchd's verdict, so a hand run killed after it
    left no trace in `status`. It is not a failure -- nothing says the job
    fails -- so the exit code stays 0 and the verdict line names it.
    """

    setUp = UnfinishedRunTest.setUp
    job = UnfinishedRunTest.job
    exec_job = UnfinishedRunTest.exec_job
    kill_a_run = UnfinishedRunTest.kill_a_run
    status = UnfinishedRunTest.status

    def test_a_killed_hand_run_is_named_and_not_failed(self):
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        self.assertEqual(self.kill_a_run().returncode, -signal.SIGKILL)
        done = self.status(last_exit=0)
        self.assertEqual(done.returncode, 0, done.stdout)
        verdict = done.stdout.splitlines()[0]
        self.assertIn("abandoned", verdict)
        self.assertIn("demo", verdict)

    def test_a_hand_run_in_progress_is_not_abandoned(self):
        self.assertEqual(self.exec_job('"true"').returncode, 0)
        self.job('"sleep 30"')
        run = subprocess.Popen(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, 4),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(run.wait)
        self.addCleanup(kill_group, run.pid)
        self.assertTrue(started(self.log, 1), "the run starts")
        done = self.status(last_exit=0)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertNotIn("abandoned", done.stdout)
        self.assertIn("hand run is in progress", done.stdout)

    def test_a_hand_run_after_a_failure_is_in_progress_not_failed(self):
        """REGRESSION (#576 review). A live hand run is the recovery in
        progress. The previous run's failure must not hide it behind FAIL."""
        self.assertEqual(self.exec_job('"exit 7"').returncode, 7)
        self.job('"sleep 30"')
        run = subprocess.Popen(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, self.bin, 4),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(run.wait)
        self.addCleanup(kill_group, run.pid)
        self.assertTrue(started(self.log, 1), "the run starts")
        done = self.status(last_exit=1)
        self.assertIn("hand run is in progress", done.stdout)
        self.assertEqual(done.returncode, 0, done.stdout)


def pids_of(command):
    """The pids of processes whose whole command line is `command`.

    Anchored and with its dots escaped, so another process whose arguments
    merely contain the text is not taken for the job's."""
    pattern = "^" + command.replace(".", "\\.") + "$"
    found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(pid) for pid in found.stdout.split()]


class JobTimeoutTest(unittest.TestCase):
    """JOB_TIMEOUT: a hung run is stopped, with its whole process group (sd:2018).

    REGRESSION. Nothing bounded a run, so a job that hung held its `flock`
    for hours and every later slot logged "skipped: previous run still
    active" and exited 0. A run past its limit now has its process group sent
    TERM, then KILL after a grace period, and fails with exit 124.

    Each workload sleeps for a duration no other test uses, so `pgrep -f`
    finds its own processes and nothing else.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.log = self.fx.folder / "logs" / "demo.log"

    def job(self, command, timeout=None):
        text = f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND={command}\n'
        if timeout is not None:
            text += f'JOB_TIMEOUT="{timeout}"\n'
        self.fx.write_job("demo", text)

    def run_job(self, env=None, timeout=20):
        return self.fx.exec_job("demo", {"CRON_JOBS_TIMEOUT_GRACE": "1", **(env or {})},
                                timeout=timeout)

    def reap(self, pattern):
        """Kill what a failing case left behind, so the next one starts clean."""
        self.addCleanup(subprocess.run, ["pkill", "-9", "-f", "^" + pattern.replace(".", "\\.") + "$"])

    def assert_gone(self, pattern):
        deadline = time.monotonic() + 5
        while pids_of(pattern) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(pids_of(pattern), [], f"{pattern} outlived the run")

    def test_a_hung_job_is_stopped_with_its_process_group(self):
        """The background child as well as the foreground one: the group,
        not the shell `bash -c` started."""
        self.reap("sleep 41.4142")
        self.job("'sleep 41.4142 & sleep 41.4142; wait'", timeout="1")
        began = time.monotonic()
        result = self.run_job()
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - began, 15)
        log = self.log.read_text()
        self.assertRegex(log, r"\[demo\] \S+ timed out after 1s \(JOB_TIMEOUT\); "
                              r"sending TERM to process group \d+")
        self.assertIn("FAILED rc=124", log)
        # TERM reached the background child too: KILL, which goes to the
        # group as well, was never needed.
        self.assertNotIn("sending KILL", log)
        self.assert_gone("sleep 41.4142")

    def test_the_lock_is_free_after_a_timeout(self):
        self.reap("sleep 43.1415")
        self.job("'sleep 43.1415'", timeout="1")
        self.assertEqual(self.run_job().returncode, 124)
        self.job('"true"', timeout="1")
        after = self.run_job()
        self.assertNotIn("skipped", after.stdout + after.stderr)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)

    def test_a_job_that_ignores_term_is_killed(self):
        self.reap("sleep 42.7182")
        self.job("\"trap '' TERM; sleep 42.7182; exit 0\"", timeout="1")
        result = self.run_job()
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertRegex(self.log.read_text(),
                         r"process group \d+ still running 1s after TERM; sending KILL")
        self.assert_gone("sleep 42.7182")

    def test_a_job_within_its_limit_keeps_its_exit_code(self):
        """PIN: the limit changes nothing for a run that ends inside it."""
        self.job('"exit 7"', timeout="30")
        result = self.run_job()
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertNotIn("timed out", self.log.read_text())

    def test_units_are_read(self):
        """`1m` is sixty seconds, not one."""
        self.job('"sleep 1.5"', timeout="1m")
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_machine_default_applies_when_the_job_sets_none(self):
        self.reap("sleep 44.1421")
        self.job("'sleep 44.1421'")
        result = self.run_job({"CRON_JOBS_JOB_TIMEOUT": "1"})
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertIn("timed out after 1s", self.log.read_text())

    def test_zero_in_the_job_turns_the_machine_default_off(self):
        self.job('"sleep 1.5"', timeout="0")
        result = self.run_job({"CRON_JOBS_JOB_TIMEOUT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_unreadable_value_fails_the_run_and_names_the_variable(self):
        self.job('"true"', timeout="soon")
        result = self.run_job()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("JOB_TIMEOUT", result.stdout + result.stderr)
        self.assertIn("FAILED rc=1", self.log.read_text())

    def test_a_terminal_on_stdin_is_not_read(self):
        """A hand-run from a terminal: the job gets /dev/null, as it does
        under launchd, rather than a terminal it would wait on."""
        controller, terminal = pty.openpty()
        self.addCleanup(os.close, controller)
        self.addCleanup(os.close, terminal)
        self.job("'read -r line || echo stdin-at-eof'", timeout="5")
        env = {"PATH": self.fx.path(), "HOME": str(self.fx.home),
               "SYSTEM_TOOLS_CONFIG": str(self.fx.config),
               "SD_REPORT_BIN": "/usr/bin/true"}
        result = subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=env, stdin=terminal, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stdin-at-eof", self.log.read_text())

    def test_the_built_in_default_is_two_hours(self):
        """PIN: a job that sets nothing is still bounded."""
        self.assertRegex(SCRIPT.read_text(), r'(?m)^DEFAULT_JOB_TIMEOUT=2h$')

    def test_a_signal_to_the_runner_still_reaches_the_job(self):
        """PIN. The job runs in a process group of its own, and launchd's
        TERM on `bootout` goes to the runner's group. The runner passes it on,
        or a stopped job would leave its workload running."""
        self.reap("sleep 45.2718")
        self.job("'sleep 45.2718'", timeout="1h")
        run = subprocess.Popen(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env={"PATH": self.fx.path(), "HOME": str(self.fx.home),
                 "SYSTEM_TOOLS_CONFIG": str(self.fx.config),
                 "SD_REPORT_BIN": "/usr/bin/true"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(run.wait)
        self.addCleanup(kill_group, run.pid)
        self.assertTrue(started(self.log), "the run starts")
        deadline = time.monotonic() + 5
        while not pids_of("sleep 45.2718") and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(run.pid, signal.SIGTERM)
        self.assert_gone("sleep 45.2718")
        # Reaped here: a group left holding only a zombie answers killpg
        # with EPERM on macOS, and the cleanup above would raise it.
        run.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
