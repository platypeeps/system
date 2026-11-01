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

import fcntl
import json
import os
import pathlib
import plistlib
import pty
import re
import shutil
import signal
import stat
import subprocess
import sys
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
LIB_BOUNDED = FOLDER.parent / "lib" / "bounded.sh"
LIB_QUARANTINE = FOLDER.parent / "lib" / "claude_quarantine.sh"

# The fake agent: one file for argv and one for the environment it was
# started with, NUL-separated so a value holding a newline cannot split. It
# writes the file `--debug-file` names, as the real binary does, and exits
# with FAKE_CLAUDE_EXIT (0 when unset), so a failing run can be staged.
# FAKE_CLAUDE_SAY, when set, is its reply on stdout; FAKE_CLAUDE_RUN, when
# set, is a command it runs after that.
FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CLAUDE_LOG.argv"
[ -z "$FAKE_CLAUDE_SAY" ] || printf '%s\\n' "$FAKE_CLAUDE_SAY"
[ -z "$FAKE_CLAUDE_RUN" ] || eval "$FAKE_CLAUDE_RUN"
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
        shutil.copy(LIB_BOUNDED, self.tmp / "lib" / "bounded.sh")
        shutil.copy(LIB_QUARANTINE, self.tmp / "lib" / "claude_quarantine.sh")
        self.config = self.tmp / "config"
        self.conf_dir = self.config / "cron-jobs"
        self.jobs = self.conf_dir / "jobs"
        self.jobs.mkdir(parents=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        # `install` builds the job launcher when it is missing (sd:6456);
        # a stub here keeps every other install test off the compiler.
        launcher = self.home / ".local" / "libexec" / "system-tools" / "job-launch"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexec \"$@\"\n")
        launcher.chmod(0o755)
        self.claude = self.tmp / "claude"
        self.claude.write_text(FAKE_CLAUDE)
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        self.claude_log = self.tmp / "claude-call"
        # `notify_failure` called bare `osascript` until sd:3454, so every
        # staged failure posted a real desktop notification: the suite fired 9
        # of them at the user per run, measured with a recording stub on PATH.
        # It stays, so a banner that comes back is caught, not shown. The stub
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


PROMPT_JOB = 'JOB_SCHEDULE="0 3 * * *"\nJOB_PROMPT="/sd-digest nightly"\n'

# A workload that SIGKILLs the runner, the shell `cron-jobs.sh exec` is. Not
# `$PPID`: the workload's parent is the perl that enforces JOB_TIMEOUT, and the
# runner is that perl's parent (sd:2018).
KILL_THE_RUNNER = "kill -9 $(ps -o ppid= -p $PPID)"
# The same, with the TERM launchd sends a job at shutdown (sd:7105).
TERM_THE_RUNNER = "kill -TERM $(ps -o ppid= -p $PPID)"


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
        self.assertEqual(self.fx.claude_argv()[:2], ["-p", "/sd-digest nightly"])

    def test_prompt_job_overrides_an_inherited_value(self):
        # REGRESSION. launchd's environment is the plist's, but a hand `run`
        # inherits the operator's shell; a stray value there must not reach
        # the job either.
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"SD_HANDOFF_RESTORE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fx.claude_env().get("SD_HANDOFF_RESTORE"), "0")

    def test_prompt_job_leaves_the_tool_shell_alone(self):
        # sd:6466. job-launch is charged for the whole job, whatever shell
        # claude's Bash tool runs, so the job sets none.
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("CLAUDE_CODE_SHELL", self.fx.claude_env())

    def test_env_sh_custom_token_reaches_claude_as_claude_code_oauth_token(self):
        # sd:5791. env.sh exports the setup token as
        # CUSTOM_CLAUDE_CODE_OAUTH_TOKEN, so a day-to-day claude keeps its
        # own login; lib/config.sh's st_shell_env maps it for the job.
        env_sh = self.fx.home / ".config" / "shell" / "env.sh"
        env_sh.parent.mkdir(parents=True)
        env_sh.write_text("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fx.claude_env().get("CLAUDE_CODE_OAUTH_TOKEN"), "tok-custom")
        self.assertNotIn("tok-custom", result.stdout + result.stderr)

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


class JobTelemetryTagTest(unittest.TestCase):
    """Each run tags its OTel resource with sd.job (sd:6944).

    claude reads OTEL_RESOURCE_ATTRIBUTES from settings.json's env, which
    beats the process environment, so a prompt job passes the tagged value
    through --settings; a command job gets it in its environment.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)

    def settings_env(self, env):
        path = self.fx.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"env": env}))

    def test_a_prompt_job_hands_claude_the_tagged_attributes(self):
        self.settings_env({"OTEL_RESOURCE_ATTRIBUTES": "service.namespace=sd-workflow,host.name=hub"})
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.fx.claude_argv()
        self.assertIn("--settings", argv, argv)
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]),
                         {"env": {"OTEL_RESOURCE_ATTRIBUTES":
                                  "service.namespace=sd-workflow,host.name=hub,sd.job=nightly"}})

    def test_without_the_telemetry_block_claude_gets_no_settings_flag(self):
        self.settings_env({"DISABLE_AUTOUPDATER": "1"})
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--settings", self.fx.claude_argv())

    def test_a_command_job_gets_the_tag_in_its_environment(self):
        marker = self.fx.tmp / "command-env"
        self.fx.write_job("plain", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="env > {marker}"\n')
        result = self.fx.exec_job("plain", {"OTEL_RESOURCE_ATTRIBUTES": "host.name=hub"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OTEL_RESOURCE_ATTRIBUTES=host.name=hub,sd.job=plain\n", marker.read_text())


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
        # sd:6466: this branch starts claude too.
        self.assertNotIn("CLAUDE_CODE_SHELL", self.fx.claude_env())

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

    def test_a_runner_ended_mid_reply_grades_the_log_not_the_reply(self):
        # sd:7105. The reply file is stdout while the agent runs, and the
        # trap fires there: the grade has to reach the job's log.
        result = self.fx.exec_job("nightly", {"FAKE_CLAUDE_SAY": "RESULT: ok",
                                              "FAKE_CLAUDE_RUN": f"{TERM_THE_RUNNER}; sleep 1"})
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        log = (self.fx.folder / "logs" / "nightly.log").read_text()
        self.assertRegex(log, r"(?m)^SD_REPORT_ATTENTION: data safe, one step failed: ")
        self.assertRegex(log, r"FAILED rc=[0-9]+")

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

    def test_the_cask_binary_wins_over_a_native_install_left_behind(self):
        # sd:4738: Claude Code is the claude-code@latest cask, so with no
        # CLAUDE_BIN and none on PATH the brew prefix's claude comes before
        # the native installer's ~/.local/bin/claude. HOMEBREW_PREFIX, brew's
        # own variable, points the prefix at this fixture.
        brew = self.fx.tmp / "brew"
        for path in (brew / "bin/claude", self.fx.home / ".local/bin/claude"):
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.fx.claude, path)
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"CLAUDE_BIN": "", "PATH": f"{self.fx.quiet}:/usr/bin:/bin",
                                              "HOMEBREW_PREFIX": str(brew)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(self.agent_line(result), rf"^\[nightly\] \S+ agent: {re.escape(str(brew / 'bin/claude'))} ->")

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

    def test_a_passed_run_keeps_its_debug_file_as_a_failed_run_does(self):
        # sd:5293 (ruling on note #24979): a passed run's trace stays too, so
        # the next locked-session stall can be compared with a passing run.
        # sd:972 removed it; the prune below still bounds the count.
        self.fx.write_job("nightly", PROMPT_JOB)
        result = self.fx.exec_job("nightly", {"FAKE_CLAUDE_EXIT": "1", "PATH": self.quiet_path()})
        self.assertEqual(result.returncode, 1, result.stderr)
        failed = self.debug_file_argument()
        self.assertTrue(failed.exists(), f"a failed run's debug file is gone: {failed}")

        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        passed = self.debug_file_argument()
        self.assertTrue(passed.exists(), f"a passed run's debug file is gone: {passed}")
        self.assertEqual(self.debug_files(), sorted([failed, passed]))

    def test_a_run_keeps_the_newest_five_debug_files(self):
        # REGRESSION (sd:972). Bounded, or a job that runs every night grows
        # the log dir without end. Four older traces and a passing run are
        # five, all kept; a sixth run prunes the oldest.
        self.fx.write_job("nightly", PROMPT_JOB)
        now = int(time.time())
        older = []
        for n in range(4):
            path = self.logs / f"nightly.debug.2026091{n}T030000Z-{100 + n}.log"
            path.write_text("old trace\n")
            os.utime(path, (now - 3600 * (6 - n), now - 3600 * (6 - n)))
            older.append(path)
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        fifth = self.debug_file_argument()
        self.assertEqual(self.debug_files(), sorted(older + [fifth]))
        os.utime(fifth, (now - 60, now - 60))
        result = self.fx.exec_job("nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        sixth = self.debug_file_argument()
        self.assertNotEqual(sixth, fifth)
        self.assertEqual(self.debug_files(), sorted(older[1:] + [fifth, sixth]))

    def test_the_prune_lists_from_the_log_dir_not_the_job_dir(self):
        # REGRESSION (sd:6437). macOS `ls` opens its cwd, and a job's
        # JOB_DIR in the ~/Documents vault drew a Full Disk Access denial
        # for /bin/ls at the end of every such run.
        calls = self.fx.tmp / "ls-cwd"
        path = self.stub_path("ls", f'#!/bin/sh\npwd >> "{calls}"\nexec /bin/ls "$@"\n')
        job_dir = self.fx.tmp / "vault"
        job_dir.mkdir()
        self.fx.write_job("nightly", PROMPT_JOB + f'JOB_DIR="{job_dir}"\n')
        result = self.fx.exec_job("nightly", {"PATH": path})
        self.assertEqual(result.returncode, 0, result.stderr)
        cwds = {pathlib.Path(line).resolve() for line in calls.read_text().splitlines()}
        self.assertEqual(cwds, {self.logs.resolve()})
        self.assertEqual(len(self.debug_files()), 1)


# The quarantine stubs record each call in $CALLS. A file's quarantine is a
# file beside it, `<path>.quarantine`; XATTR_FAIL fails the read of the
# names; RACED makes the delete find the flag already gone, as when another
# job cleared it first. Every check passes only for the path in GOOD;
# BAD_SIGNATURE fails the verify.
QUARANTINE_STUBS = {
    "xattr": r"""#!/bin/sh
echo "xattr $*" >> "$CALLS"
case $1 in
  -d) [ "$2" = com.apple.quarantine ] && rm "$3.quarantine"
      [ -z "$RACED" ] || { echo "xattr: $3: No such xattr: $2" >&2; exit 1; } ;;
  -*) exit 2 ;;
  *) [ -z "$XATTR_FAIL" ] || exit 1
     [ ! -e "$1.quarantine" ] || echo com.apple.quarantine ;;
esac
""",
    "codesign": r"""#!/bin/sh
echo "codesign $*" >> "$CALLS"
for last; do :; done
[ "$last" = "$GOOD" ] || exit 1
case $1 in
  --verify) [ -z "$BAD_SIGNATURE" ] ;;
  -dv) echo "TeamIdentifier=Q6L2SF6YDW" >&2 ;;
  *) exit 2 ;;
esac
""",
    "spctl": r"""#!/bin/sh
echo "spctl $*" >> "$CALLS"
for last; do :; done
[ "$last" = "$GOOD" ] || exit 3
echo "source=Notarized Developer ID" >&2
""",
}

# The cask's claude: it records whether its quarantine was still there when
# it started, which is what Gatekeeper would have asked about. It starts
# through the link, so $0 is the link.
CASK_CLAUDE = r"""#!/bin/sh
if [ -e "$(readlink "$0").quarantine" ]; then echo "claude ran quarantined" >> "$CALLS"
else echo "claude ran" >> "$CALLS"; fi
"""


class AgentQuarantineTest(unittest.TestCase):
    """The runner starts no quarantined cask claude (sd:4815).

    A cask installed by hand kept com.apple.quarantine until machine-setup's
    nightly, and six agent jobs before it each waited 2 h on a Gatekeeper
    prompt. The runner now runs lib/claude_quarantine.sh first: it clears a
    flag whose checks pass, and a failed check fails the job at once.
    CLAUDE_BIN links into a Caskroom, as the cask's bin/claude does.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.stubs = self.fx.tmp / "stubs"
        self.stubs.mkdir()
        for name, body in QUARANTINE_STUBS.items():
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        caskroom = self.fx.tmp / "Caskroom/claude-code@latest"
        self.old = caskroom / "2.1.300/claude"
        self.cask = caskroom / "2.1.301/claude"
        for binary in (self.old, self.cask):
            binary.parent.mkdir(parents=True)
            binary.write_text(CASK_CLAUDE)
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        self.link = self.fx.tmp / "brew-bin/claude"
        self.link.parent.mkdir()
        self.link.symlink_to(self.cask)
        self.quarantine = pathlib.Path(f"{self.cask}.quarantine")
        self.quarantine.touch()
        self.calls = self.fx.tmp / "calls.log"
        self.fx.write_job("nightly", PROMPT_JOB)

    def run_job(self, **extra):
        env = {"PATH": self.fx.path(self.stubs), "CALLS": str(self.calls),
               "CLAUDE_BIN": str(self.link), "GOOD": str(self.cask), **extra}
        started = time.monotonic()
        result = self.fx.exec_job("nightly", env)
        self.elapsed = time.monotonic() - started
        self.log = (self.fx.folder / "logs/nightly.log").read_text()
        return result

    def calls_of(self, tool):
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        return [line for line in lines if line.split(" ", 1)[0] == tool]

    def assert_refused(self, result, why):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.calls_of("claude"), [], self.log)
        self.assertTrue(self.quarantine.exists(), self.log)
        self.assertNotIn(f"xattr -d com.apple.quarantine {self.cask}", self.calls_of("xattr"))
        self.assertRegex(self.log, rf"\[nightly\] \S+ claude quarantine {why}")
        self.assertRegex(self.log, r"\[nightly\] \S+ FAILED rc=1")
        # Fails at once, not at the job's 2 h limit.
        self.assertLess(self.elapsed, 30)

    def test_a_verified_quarantine_is_cleared_before_the_agent_runs(self):
        # REGRESSION (sd:4815). The runner started the quarantined binary.
        result = self.run_job()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_of("claude"), ["claude ran"], self.log)
        self.assertFalse(self.quarantine.exists(), self.log)
        self.assertRegex(self.log, r"\[nightly\] \S+ claude quarantined; signed, notarized, "
                                   r"team Q6L2SF6YDW: clearing")
        # Each check's step line reaches the job log.
        self.assertRegex(self.log, r"\[step [\d:]+\] spctl --assess .*\(bound 120s\)")

    def test_a_flag_another_job_cleared_first_runs_the_agent(self):
        # REGRESSION (sd:6482). Three jobs share the 12:00 slot; one cleared
        # the flag, and the others' delete found it gone and failed the job.
        result = self.run_job(RACED="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_of("claude"), ["claude ran"], self.log)
        self.assertNotIn("quarantine kept", self.log)

    def test_a_failed_check_fails_the_job_at_once(self):
        # REGRESSION (sd:4815). The runner started the quarantined binary.
        result = self.run_job(BAD_SIGNATURE="1")

        self.assert_refused(result, f"kept on {re.escape(str(self.cask))}: the signature does not verify")

    def test_checks_that_pass_for_another_version_fail_the_job(self):
        # REGRESSION (sd:4815). Evidence: the checks are of the linked binary.
        result = self.run_job(GOOD=str(self.old))

        self.assert_refused(result, f"kept on {re.escape(str(self.cask))}: the signature does not verify")

    def test_an_unreadable_flag_fails_the_job(self):
        # REGRESSION (sd:4815). An xattr that cannot read is unknown, not clear.
        result = self.run_job(XATTR_FAIL="1")

        self.assert_refused(result, f"unknown: xattr could not read {re.escape(str(self.cask))}")
        self.assertEqual(self.calls_of("codesign"), [])

    def test_no_quarantine_runs_no_check_and_logs_nothing(self):
        # PIN. Nothing to clear changes nothing and says nothing.
        self.quarantine.unlink()
        result = self.run_job()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_of("claude"), ["claude ran"])
        self.assertEqual(self.calls_of("codesign") + self.calls_of("spctl"), [])
        self.assertNotIn("quarantin", self.log)

    def test_a_claude_outside_the_cask_is_left_alone(self):
        # PIN. Only the cask's binary is checked; the fixture's fake is not one.
        result = self.run_job(CLAUDE_BIN=str(self.fx.claude))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.fx.claude_was_called())
        self.assertEqual(self.calls_of("xattr") + self.calls_of("codesign") + self.calls_of("spctl"), [])
        self.assertNotIn("quarantin", self.log)


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

    def test_a_retired_jobs_last_failure_is_not_outstanding(self):
        # sd:3212. A job retired after a failed run never logs `done` again,
        # so its failure stayed under "recent failures" for good. No plist
        # here means nothing to resolve; an installed job's failure still shows.
        self.install()
        (self.fx.folder / "logs" / "failures.log").write_text(
            "2026-10-09T04:30:02-0600 retired-job FAILED rc=1 (log: logs/retired-job.log)\n"
            "2026-10-09T05:00:00-0600 demo FAILED rc=1 (log: logs/demo.log)\n")
        out = self.status().stdout
        self.assertNotIn("retired-job FAILED", out)
        self.assertIn("demo FAILED", out)

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

    def test_the_shared_prefix_reaches_the_job(self):
        # sd:3990. The prefix lives once in <config>/shared.env, not in this
        # tool's .env; the job still sees it, as the pack's tools read it from
        # the environment.
        (self.fx.config / "shared.env").write_text('SYSTEM_TOOLS_LABEL_PREFIX="example.test"\n')
        self.assertEqual(self.job_env().get("SYSTEM_TOOLS_LABEL_PREFIX"), "example.test")

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
        # directories, including the extra ones. Where machine-setup records
        # a profile, --all refuses instead (sd:3186, HostJobsTest).
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
            f'  print) [ -e \'{held}\' ] || exit 1\n'
            f'    printf \'\\tstdout path = %s\\n\' \'{self.fx.folder}/logs/demo.log\' ;;\n', 1)
        launchctl.write_text(text)
        launchctl.chmod(launchctl.stat().st_mode | stat.S_IXUSR)
        result = self.run_script("install", "demo", env={"CRON_TEST_HELD": "1", "PATH": self.fx.path(stub)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls.read_text().splitlines()
        after = calls[calls.index(next(c for c in calls if c.startswith("launchctl bootout"))) + 1:]
        before_bootstrap = after[:after.index(self.bootstraps()[0])]
        self.assertGreaterEqual(sum(c.startswith("launchctl print") for c in before_bootstrap), 2, calls)
        self.assertFalse(held.exists())


# What a `launchctl` stub asks of a lock file: `held` when another open file
# holds its flock, `free` when this one could take it. A fresh open is a file
# description of its own, so the stub's inherited fd 8 does not answer for it.
PROBE_LOCK = ("import fcntl, sys\n"
              "f = open(sys.argv[1], 'a')\n"
              "try:\n"
              "    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
              "    print('free')\n"
              "except BlockingIOError:\n"
              "    print('held')\n")


class InstallRespectsTheJobLockTest(unittest.TestCase):
    """sd:3317. `install` and `uninstall` leave a running job alone.

    REGRESSION. Both booted the label out whenever launchd held it, and
    `bootout` sends TERM to the run: a reinstall on a satellite killed a lane
    run mid-ship, and the lane recorded the runner dead with its entry running.
    They now hold the job's own lock (the `flock` a run takes, sd:1251) from
    before the plist is written until the label is back, and refuse while a run
    holds it. launchctl is stub_bin's, with launchd's label state kept in a
    file per label: loaded until `bootout`, loaded again on `bootstrap`.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.calls = self.fx.tmp / "launchctl-calls"
        self.calls.write_text("")
        self.fx.write_job("demo", 'JOB_SCHEDULE="*/10 * * * *"\nJOB_COMMAND="true"\n')
        self.agents = self.fx.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)

    def plist(self, job="demo"):
        return self.agents / f"{LABEL_PREFIX}.cron.{job}.plist"

    def lock(self, job="demo"):
        return self.fx.folder / "logs" / f".{job}.flock"

    def hold(self, job="demo"):
        """Take the job's lock the way a run in progress holds it."""
        held = open(self.lock(job), "a")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def lock_is_free(self, job="demo"):
        probe = subprocess.run([sys.executable, "-c", PROBE_LOCK, str(self.lock(job))],
                               capture_output=True, text=True, check=True)
        return probe.stdout.strip() == "free"

    def launchd(self, name="launchd-bin", on_bootout="", on_bootstrap="", stdout=True):
        """stub_bin's launchctl in a folder of its own, holding every label
        until `bootout` and again after `bootstrap`. `on_*` run first. Its
        `print` names the label's log unless `stdout` is false."""
        folder = self.fx.tmp / name
        if folder.exists():
            return folder
        folder.mkdir()
        launchctl = folder / "launchctl"
        gone =f"{self.fx.tmp}/gone."
        launchctl.write_text((stub_bin(self.fx.tmp) / "launchctl").read_text().replace(
            'case "$1" in\n',
            f'gone="{gone}$(basename "${{3:-$2}}" .plist)"\n'
            'case "$1" in\n'
            '  print) [ ! -e "$gone" ] || exit 1\n'
            + (f'    printf \'\\tstdout path = %s/%s.log\\n\' "${{CRON_TEST_STDOUT_DIR:-{self.fx.folder}/logs}}" "${{2##*.cron.}}"\n'
               if stdout else '') +
            '    exit 0 ;;\n'
            f'  bootout) {on_bootout}touch "$gone"; exit 0 ;;\n'
            f'  bootstrap) {on_bootstrap}rm -f "$gone"; exit 0 ;;\n', 1))
        launchctl.chmod(launchctl.stat().st_mode | stat.S_IXUSR)
        return folder

    def run_script(self, *args, path=None, env=None):
        env = {"PATH": path or self.fx.path(self.launchd()),
               "HOME": str(self.fx.home),
               "SYSTEM_TOOLS_CONFIG": str(self.fx.config),
               "CRON_TEST_CALLS": str(self.calls),
               "PROBE_LOCK": PROBE_LOCK, **(env or {})}
        return subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def bootouts(self):
        return [line for line in self.calls.read_text().splitlines() if line.startswith("launchctl bootout")]

    def test_install_refuses_while_a_run_holds_the_lock(self):
        self.plist().write_text("the plist launchd holds\n")
        self.hold()
        result = self.run_script("install", "demo")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("demo: a run is in progress; retry after it ends", result.stderr)
        self.assertNotIn("installed:", out)
        self.assertEqual(self.bootouts(), [])
        self.assertNotIn("launchctl bootstrap", self.calls.read_text())
        # Unwritten, so `verify` still reads it STALE and the next install retries.
        self.assertEqual(self.plist().read_text(), "the plist launchd holds\n")

    def test_uninstall_refuses_while_a_run_holds_the_lock(self):
        self.plist().write_text("the plist launchd holds\n")
        self.hold()
        result = self.run_script("uninstall", "demo")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("demo: a run is in progress; retry after it ends", result.stderr)
        self.assertNotIn("uninstalled:", out)
        self.assertEqual(self.bootouts(), [])
        self.assertTrue(self.plist().exists())

    def test_uninstall_all_skips_only_the_busy_job(self):
        self.fx.write_job("other", 'JOB_SCHEDULE="0 * * * *"\nJOB_COMMAND="true"\n')
        self.plist().write_text("demo\n")
        self.plist("other").write_text("other\n")
        self.hold()
        result = self.run_script("uninstall", "--all")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, out)
        self.assertIn("uninstalled: other", result.stdout)
        self.assertIn("uninstall failed for: demo", result.stderr)
        self.assertEqual(self.bootouts(), [f"launchctl bootout gui/{os.getuid()}/{LABEL_PREFIX}.cron.other"])
        self.assertTrue(self.plist().exists())
        self.assertFalse(self.plist("other").exists())

    def test_install_all_skips_only_the_busy_job(self):
        self.fx.write_job("other", 'JOB_SCHEDULE="0 * * * *"\nJOB_COMMAND="true"\n')
        self.hold()
        result = self.run_script("install", "--all")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, out)
        self.assertIn("installed: other", result.stdout)
        self.assertIn("install failed for: demo", result.stderr)
        self.assertEqual(self.bootouts(), [f"launchctl bootout gui/{os.getuid()}/{LABEL_PREFIX}.cron.other"])
        self.assertFalse(self.plist().exists())

    def test_without_a_run_both_proceed_as_before(self):
        """PIN: a free lock changes nothing, and neither leaves it held."""
        installed = self.run_script("install", "demo")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn("installed: demo", installed.stdout)
        self.assertTrue(self.lock_is_free())
        uninstalled = self.run_script("uninstall", "demo")
        self.assertEqual(uninstalled.returncode, 0, uninstalled.stdout + uninstalled.stderr)
        self.assertIn("uninstalled: demo", uninstalled.stdout)
        self.assertEqual(len(self.bootouts()), 2, self.calls.read_text())
        self.assertFalse(self.plist().exists())
        self.assertTrue(self.lock_is_free())

    def test_the_lock_is_held_across_bootout_and_bootstrap_and_released_per_job(self):
        """A slot that fires mid-install skips rather than starting a run the
        bootout would kill, and `--all` lets each job's lock go once that job
        is back in launchd."""
        self.fx.write_job("other", 'JOB_SCHEDULE="0 * * * *"\nJOB_COMMAND="true"\n')
        probe = (f'echo "$1 $(basename "${{3:-$2}}") $(\'{sys.executable}\' -c "$PROBE_LOCK" '
                 f'\'{self.lock()}\')" >> \'{self.fx.tmp}/probes\'; ')
        result = self.run_script("install", "--all", path=self.fx.path(
            self.launchd("probe-bin", on_bootout=probe, on_bootstrap=probe)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probes = (self.fx.tmp / "probes").read_text().splitlines()
        self.assertEqual(probes, [
            f"bootout {LABEL_PREFIX}.cron.demo held",
            f"bootstrap {LABEL_PREFIX}.cron.demo.plist held",
            f"bootout {LABEL_PREFIX}.cron.other free",
            f"bootstrap {LABEL_PREFIX}.cron.other.plist free",
        ])

    def test_an_install_killed_after_bootout_leaves_no_lock_and_a_rerun_finishes(self):
        """The step between unload and load: the kernel drops the lock with
        the killed shell, and the next install bootstraps the unloaded label."""
        killed = self.run_script("install", "demo", path=self.fx.path(
            self.launchd("kill-bin", on_bootout="touch \"$gone\"; kill -9 $PPID; ")))
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertNotIn("launchctl bootstrap", self.calls.read_text())
        self.assertTrue(self.lock_is_free())
        rerun = self.run_script("install", "demo")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("installed: demo", rerun.stdout)
        self.assertIn("launchctl bootstrap", self.calls.read_text())

    def test_a_label_another_checkout_installed_is_judged_by_that_checkouts_lock(self):
        """REGRESSION (codex pre-review). The loaded plist runs the checkout
        that installed it, and that checkout's run locks its own logs/: a
        worktree, or a clone that moved. This checkout's lock is free."""
        other = self.fx.tmp / "other-checkout" / "local-cron-jobs" / "logs"
        other.mkdir(parents=True)
        held = open(other / ".demo.flock", "a")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.plist().write_text("the plist launchd holds\n")
        for verb in ("install", "uninstall"):
            result = self.run_script(verb, "demo", env={"CRON_TEST_STDOUT_DIR": str(other)})
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("demo: a run is in progress; retry after it ends", result.stderr)
        self.assertEqual(self.bootouts(), [])
        self.assertEqual(self.plist().read_text(), "the plist launchd holds\n")
        self.assertTrue(self.lock_is_free())

    def test_a_loaded_label_that_names_no_log_is_refused(self):
        """Every plist this script wrote names its log; without one the run's
        lock is unknown, and unknown is not free."""
        self.plist().write_text("the plist launchd holds\n")
        for verb in ("install", "uninstall"):
            result = self.run_script(verb, "demo", path=self.fx.path(self.launchd("bare-bin", stdout=False)))
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("launchd names no log", result.stderr)
        self.assertEqual(self.bootouts(), [])

    def test_a_label_whose_log_folder_is_gone_is_replaced(self):
        """PIN: a removed worktree's label stays replaceable. launchd cannot
        start a run whose log folder is gone, so this checkout's lock serves."""
        result = self.run_script("install", "demo",
                                 env={"CRON_TEST_STDOUT_DIR": str(self.fx.tmp / "removed" / "logs")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("installed: demo", result.stdout)
        self.assertEqual(len(self.bootouts()), 1)

    def test_a_lock_it_cannot_test_is_not_read_as_free(self):
        """A probe that fails says nothing: perl not running is not "no run"."""
        folder = self.fx.tmp / "no-perl-bin"
        folder.mkdir()
        perl = folder / "perl"
        perl.write_text("#!/bin/sh\nexit 2\n")
        perl.chmod(perl.stat().st_mode | stat.S_IXUSR)
        self.plist().write_text("the plist launchd holds\n")
        for verb in ("install", "uninstall"):
            result = self.run_script(verb, "demo", path=self.fx.path(folder, self.launchd(f"{verb}-bin")))
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("could not test the lock", result.stderr)
        self.assertEqual(self.bootouts(), [])
        self.assertEqual(self.plist().read_text(), "the plist launchd holds\n")


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

    def test_the_plist_runs_the_job_at_interactive_priority(self):
        """launchd throttles a job with no ProcessType (Standard): repo-sync's
        gate suite ran 155 tests in 237 s from a terminal but reached 142 in
        32 min as a launchd job, so lane gates hit their hour limit (sd:6504;
        Background was worse still, sd:3173). A job's plist says Interactive."""
        self.fx.write_job("demo", self.JOB.format("true"))
        result = self.run_script("install", "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.fx.home / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.cron.demo.plist"
        self.assertEqual("Interactive", plistlib.loads(plist.read_bytes()).get("ProcessType"))

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

    def test_a_recorded_profile_refuses_install_all_and_verify_all(self):
        # REGRESSION (sd:3186). --all read every shared job, not the set the
        # machine-setup profile names, so a satellite loaded every hub job.
        # Where a profile is recorded, the cron stage owns that set: --all
        # refuses, installs nothing and reports nothing missing.
        for name in ("one", "two", "three", "four", "five"):
            self.fx.write_job(name, self.JOB.format("true"))
        state = self.fx.home / ".config" / "machine-setup"
        state.mkdir(parents=True)
        (state / "profile").write_text("satellite\n")
        for args in (("install", "--all"), ("install", "--every"), ("verify", "--all"), ("verify",)):
            result = self.run_script(*args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn("machine-setup.sh update cron", result.stderr, args)
            self.assertNotIn("missing", result.stdout, args)
        self.assertEqual(self.installed(), [])
        # One named job still installs; uninstall --all still clears.
        self.assertEqual(self.run_script("install", "one").returncode, 0)
        self.assertEqual(self.installed(), [f"{LABEL_PREFIX}.cron.one.plist"])
        self.assertEqual(self.run_script("uninstall", "--all").returncode, 0)
        self.assertEqual(self.installed(), [])

    def test_a_profile_override_or_a_state_folder_it_cannot_read_refuses_too(self):
        # MACHINE_SETUP_PROFILE names the profile for one run, as it does for
        # machine-setup. A state folder this run cannot search may hold a
        # profile: that is unknown, not "no profile", so --all refuses.
        self.fx.write_job("one", self.JOB.format("true"))
        env = {**self.env(), "MACHINE_SETUP_PROFILE": "satellite"}
        result = subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "install", "--all"],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        state = self.fx.tmp / "state"
        state.mkdir()
        state.chmod(0o600)
        self.addCleanup(state.chmod, 0o700)
        env = {**self.env(), "MACHINE_SETUP_STATE": str(state)}
        result = subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "install", "--all"],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
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
        # The log the plist names, in the fixture's own logs/ unless a test
        # moves it: install and uninstall lock the folder it sits in (sd:3317).
        "    printf '\\tstdout path = %s/%s.log\\n' \"${CRON_TEST_STDOUT_DIR:-" + str(tmp / "local-cron-jobs" / "logs") + "}\" \"${2##*.cron.}\"\n"
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


# The fake `sd`: one line per call, its arguments, then it exits with
# FAKE_SD_EXIT (0 when unset). FAKE_SD_SLEEP, when set, makes it hang first.
FAKE_SD = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SD_LOG"
[ -z "$FAKE_SD_SLEEP" ] || sleep "$FAKE_SD_SLEEP"
exit "${FAKE_SD_EXIT:-0}"
"""


class RunReportTest(unittest.TestCase):
    """Every run that owes an outcome files one run report, and the report
    cannot hold the run up or change its exit code (sd:3292)."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.sd = self.fx.tmp / "sd"
        self.sd.write_text(FAKE_SD)
        self.sd.chmod(self.sd.stat().st_mode | stat.S_IXUSR)
        self.sd_log = self.fx.tmp / "sd-calls"

    def exec_job(self, job="demo", timeout=60, **extra):
        return self.fx.exec_job(job, {"SD_REPORT_BIN": str(self.sd),
                                      "FAKE_SD_LOG": str(self.sd_log), **extra},
                                timeout=timeout)

    def reports(self):
        if not self.sd_log.exists():
            return []
        return self.sd_log.read_text().splitlines()

    def test_a_run_the_trap_ends_files_its_report(self):
        # REGRESSION. An unusable JOB_DIR ends the run through the EXIT trap,
        # which wrote the outcome and filed no report, so the failure never
        # reached Tasks.
        missing = self.fx.tmp / "no-such-directory"
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\n'
                                  'JOB_COMMAND="true"\n'
                                  f'JOB_DIR="{missing}"\n')
        result = self.exec_job()
        self.assertEqual(result.returncode, 1, result.stderr)
        reports = self.reports()
        self.assertEqual(len(reports), 1, reports)
        self.assertRegex(reports[0], r"^reports ingest demo --run-id [0-9TZ]+-[0-9]+ "
                                     r"--started \S+ --ended \S+ --exit-code 1 --offset 0 ")

    def test_a_deleted_definition_files_its_report(self):
        # REGRESSION. `load_job` exits through the trap too.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        self.assertEqual(self.exec_job().returncode, 0)
        (self.fx.jobs / "demo.job").unlink()
        self.assertNotEqual(self.exec_job().returncode, 0)
        reports = self.reports()
        self.assertEqual(len(reports), 2, reports)
        self.assertIn("--exit-code 0 ", reports[0])
        self.assertNotIn("--exit-code 0 ", reports[1])

    def test_a_run_that_reaches_the_end_files_one_report_only(self):
        # PIN. The trap must not file a second report for a run whose own end
        # already filed one.
        for command, code in (("true", 0), ("exit 7", 7)):
            with self.subTest(command=command):
                self.sd_log.unlink(missing_ok=True)
                self.fx.write_job("demo", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="{command}"\n')
                self.assertEqual(self.exec_job().returncode, code)
                reports = self.reports()
                self.assertEqual(len(reports), 1, reports)
                self.assertIn(f"--exit-code {code} ", reports[0])

    def test_a_hung_report_is_bounded_and_keeps_the_exit_code(self):
        # REGRESSION. `sd reports ingest` ran unbounded, so a hung database
        # call held the run, and its lock, for as long as it hung.
        for command, code in (("true", 0), ("exit 7", 7)):
            with self.subTest(command=command):
                self.fx.write_job("demo", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="{command}"\n')
                started = time.monotonic()
                result = self.exec_job(timeout=20, FAKE_SD_SLEEP="60",
                                       CRON_JOBS_REPORT_TIMEOUT="1")
                self.assertLess(time.monotonic() - started, 15)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn("report was not recorded", result.stderr)

    def test_a_failed_report_keeps_the_exit_code(self):
        # PIN. A report that fails is logged; the job's own code stands.
        for command, code in (("true", 0), ("exit 7", 7)):
            with self.subTest(command=command):
                self.fx.write_job("demo", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="{command}"\n')
                result = self.exec_job(FAKE_SD_EXIT="3")
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn("report was not recorded", result.stderr)

    def test_a_refused_report_runs_the_job_once_and_leaves_no_lock(self):
        # PIN (sd:6355). Pack sd:6044 moved this case here from the
        # dashboard suite: a database that refuses the report must not
        # run the job twice, change its exit, leave its lock or file a
        # failure line for a run that succeeded.
        launchctl = self.fx.quiet / "launchctl"
        launchctl_calls = self.fx.tmp / "launchctl-calls"
        launchctl.write_text(f"#!/bin/sh\necho \"$*\" >> '{launchctl_calls}'\nexit 1\n")
        launchctl.chmod(launchctl.stat().st_mode | stat.S_IXUSR)
        logs = self.fx.folder / "logs"
        (logs / "demo.log").write_text("PREVIOUS RUN\n")
        self.fx.write_job("demo", 'JOB_SCHEDULE="* * * * *"\n'
                                  "JOB_COMMAND=\"printf 'REPORT BODY\\n'\"\n")
        result = self.exec_job(FAKE_SD_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((logs / "demo.log").read_text().count("REPORT BODY"), 1)
        argv = self.reports()[0].split()
        self.assertEqual(argv[:3], ["reports", "ingest", "demo"])
        self.assertEqual(argv[argv.index("--offset") + 1], str(len("PREVIOUS RUN\n")))
        self.assertIn("report was not recorded", result.stderr)
        self.assertIn("print gui/", launchctl_calls.read_text())
        self.assertFalse((logs / ".demo.lock").exists())
        self.assertFalse((logs / "failures.log").exists())
        self.assertEqual(len(self.fx.notifications()), 1, self.fx.notifications())
        self.assertIn("Job 'demo': report not recorded", self.fx.notifications()[0])
        self.fx.write_job("demo", 'JOB_SCHEDULE="* * * * *"\nJOB_COMMAND="exit 7"\n')
        result = self.exec_job(FAKE_SD_EXIT="1")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(len(self.reports()), 2)
        self.assertEqual((logs / "failures.log").read_text().count("FAILED rc=7"), 1)
        self.assertFalse((logs / ".demo.lock").exists())

    def test_a_prompt_jobs_reply_carries_its_attention_lines_to_the_report(self):
        # PIN (sd:5000). A prompt job raises attention on Today by writing
        # `SD_REPORT_ATTENTION:` lines in its reply (the vault's intel-weekly
        # routine does). The reply must reach the log the report reads, from
        # the report's offset, each line whole and at its start, as
        # `reports ingest` matches it, with or without JOB_RESULT_OK.
        reply = ("# Weekly synthesis filed\n\n- 4 trends\n"
                 "SD_REPORT_ATTENTION: intel-weekly: LIKELY: T2 links two unrelated launches\n"
                 "SD_REPORT_ATTENTION: intel-weekly: adversarial gate not run: codex timed out\n"
                 "RESULT: ok")
        log = self.fx.folder / "logs" / "demo.log"
        for extra in ("", 'JOB_RESULT_OK="^RESULT: ok"\n'):
            with self.subTest(result_ok=bool(extra)):
                self.sd_log.unlink(missing_ok=True)
                self.fx.write_job("demo", PROMPT_JOB + extra)
                self.assertEqual(self.exec_job(FAKE_CLAUDE_SAY=reply).returncode, 0)
                offset = int(re.search(r"--offset (\d+) ", self.reports()[0]).group(1))
                marks = [line for line in log.read_bytes()[offset:].splitlines()
                         if line.startswith(b"SD_REPORT_ATTENTION: ")]
                self.assertEqual(marks, [b"SD_REPORT_ATTENTION: intel-weekly: LIKELY: T2 links two unrelated launches",
                                         b"SD_REPORT_ATTENTION: intel-weekly: adversarial gate not run: codex timed out"])

    def lost_report_case(self, first_job):
        """One run whose report is not recorded, then two that are: the
        ingest offsets of the three, the log, and the pushes."""
        curl_log = self.fx.tmp / "curl-calls.txt"
        curl = self.fx.quiet / "curl"
        curl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{curl_log}'\n")
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        (self.fx.conf_dir / "notify.conf").write_text("NTFY_TOPIC=example-topic\n")
        log = self.fx.folder / "logs" / "demo.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("[demo] an older run\n")
        self.fx.write_job("demo", first_job)
        self.exec_job(FAKE_SD_EXIT="3")
        pushes = (self.fx.notifications(), curl_log.read_text() if curl_log.exists() else "")
        # A producer that reports a finding once and moves its baseline.
        flag = self.fx.tmp / "baseline-moved"
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\n'
                                  f'JOB_COMMAND="[ -f {flag} ] || {{ touch {flag}; echo SD_REPORT_ATTENTION: new agent; }}"\n')
        self.exec_job()
        self.exec_job()
        offsets = [int(re.search(r"--offset (\d+) ", line).group(1)) for line in self.reports()]
        return offsets, log.read_text().encode(), pushes

    def test_a_lost_report_pushes_and_the_next_report_starts_at_the_lost_run(self):
        # REGRESSION (sd:3537). A failed ingest returned 0 and nothing kept
        # the run's findings: the producer had moved its baseline, so the
        # next run found nothing and the finding was lost.
        offsets, log, (banners, posts) = self.lost_report_case(
            'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="echo SD_REPORT_ATTENTION: new agent; touch ' +
            str(self.fx.tmp / "baseline-moved") + '"\n')
        self.assertEqual(len(banners), 1, banners)
        self.assertIn("ntfy.sh/example-topic", posts)
        self.assertEqual(offsets[1], offsets[0], offsets)
        self.assertIn(b"SD_REPORT_ATTENTION: new agent", log[offsets[1]:])
        self.assertGreater(offsets[2], offsets[1], "a recorded report clears the carry")

    def test_a_lost_report_from_the_trap_pushes_and_carries_too(self):
        # REGRESSION (sd:3537). The EXIT trap's report is the second caller.
        offsets, _, (banners, posts) = self.lost_report_case(
            'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n'
            f'JOB_DIR="{self.fx.tmp / "no-such-directory"}"\n')
        self.assertEqual(len(banners), 1, banners)
        self.assertIn("ntfy.sh/example-topic", posts)
        self.assertEqual(offsets[1], offsets[0], offsets)
        self.assertGreater(offsets[2], offsets[1], offsets)

    def test_a_recorded_report_pushes_nothing_and_carries_nothing(self):
        # PIN. Only a lost report pushes.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="echo hi"\n')
        self.exec_job()
        self.exec_job()
        offsets = [int(re.search(r"--offset (\d+) ", line).group(1)) for line in self.reports()]
        self.assertGreater(offsets[1], offsets[0], offsets)
        self.assertEqual(self.fx.notifications(), [])


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
    """A failed run is recorded and reported; only one at risk pushes (sd:5997).

    sd:3454 moved every failure to the run's report. sd:5997 brings back one
    push, for the critical case: a failure that did not grade itself with
    `SD_REPORT_ATTENTION: data safe, one step failed: <step>` (ruling on
    sd:6030). It goes through local-notify, once until a run is not at risk.
    The fixture's recording `osascript` and `curl` catch any other post, on
    both PATHs a run can be started with: the plain one `Fixture.exec_job`
    builds and `stub_env`'s.
    """

    SAFE = "SD_REPORT_ATTENTION: data safe, one step failed: NAS copy"

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="exit 3"\n')
        # A notify.conf of the kind the wrapper used to read; nothing may use it.
        (self.fx.conf_dir / "notify.conf").write_text("NTFY_TOPIC=example-topic\n")
        self.curl_log = self.fx.tmp / "curl-calls.txt"
        curl = self.fx.quiet / "curl"
        curl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{self.curl_log}'\n")
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        # local-notify beside the copy: one line per call, its words joined
        # by `|`, and it exits NOTIFY_EXIT.
        notify = self.fx.tmp / "local-notify" / "notify.sh"
        notify.parent.mkdir()
        self.notify_log = self.fx.tmp / "notify-calls.txt"
        notify.write_text("#!/bin/sh\n"
                          f"(IFS='|'; printf '%s\\n' \"$*\") >> '{self.notify_log}'\n"
                          'exit "${NOTIFY_EXIT:-0}"\n')

    def pushes(self):
        return self.notify_log.read_text().splitlines() if self.notify_log.exists() else []

    def job(self, command):
        self.fx.write_job("demo", f'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="{command}"\n')

    def assert_recorded_and_no_other_post(self):
        self.assertEqual(self.fx.notifications(), [])
        self.assertFalse(self.curl_log.exists(), self.curl_log.read_text() if self.curl_log.exists() else "")
        self.assertIn("demo FAILED rc=3", (self.fx.folder / "logs" / "failures.log").read_text())

    def test_a_failure_pushes_once_until_a_run_is_not_at_risk(self):
        for _ in range(2):
            self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        self.assert_recorded_and_no_other_post()
        pushes = self.pushes()
        self.assertEqual(len(pushes), 1, pushes)
        words = pushes[0].split("|")
        self.assertEqual(words[:6], ["-t", f"demo failed on {host_name()}: data at risk",
                                     "-p", "high", "-c", "ntfy"], words)
        self.assertIn("exit 3", words[6])
        self.job("true")
        self.assertEqual(self.fx.exec_job("demo").returncode, 0)
        self.job("exit 3")
        self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        self.assertEqual(len(self.pushes()), 2, self.pushes())

    def test_a_failure_graded_data_safe_pushes_nothing(self):
        self.job(f"echo {self.SAFE}; exit 3")
        self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        self.assert_recorded_and_no_other_post()
        self.assertEqual(self.pushes(), [])

    def test_a_grade_quoted_inside_a_line_does_not_count(self):
        self.job(f"echo quoted: {self.SAFE}; exit 3")
        self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        self.assertEqual(len(self.pushes()), 1, self.pushes())

    def test_an_older_runs_grade_does_not_cover_this_run(self):
        # A lost report starts this run's report at the older run's offset
        # (sd:3537), so the report's span holds that run's grade. The push
        # reads only this run's own lines.
        log = self.fx.folder / "logs" / "demo.log"
        log.write_text(f"[demo] an older run\n{self.SAFE}\n")
        (self.fx.folder / "logs" / ".demo.unreported").write_text("0\n")
        self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        self.assertEqual(len(self.pushes()), 1, self.pushes())

    def test_a_push_that_failed_is_tried_by_the_next_failing_run(self):
        for _ in range(2):
            run = self.fx.exec_job("demo", {"NOTIFY_EXIT": "1"})
            self.assertEqual(run.returncode, 3)
            self.assertIn("at-risk push did not go out", run.stderr)
        self.assertEqual(len(self.pushes()), 2, self.pushes())

    def test_a_missing_notifier_is_named_and_keeps_the_exit_code(self):
        (self.fx.tmp / "local-notify" / "notify.sh").unlink()
        run = self.fx.exec_job("demo")
        self.assertEqual(run.returncode, 3)
        self.assertIn("at-risk push did not go out", run.stderr)

    def test_a_failure_the_trap_reports_pushes_too(self):
        # JOB_DIR that does not exist: the run ends before cmd_exec's end,
        # and exec_exit writes its outcome and report.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n'
                                  f'JOB_DIR="{self.fx.tmp / "no-such-directory"}"\n')
        self.assertNotEqual(self.fx.exec_job("demo").returncode, 0)
        self.assertEqual(len(self.pushes()), 1, self.pushes())

    def test_a_runner_ended_by_a_signal_is_interrupted_not_at_risk(self):
        # sd:7105. A shutdown sends launchd's TERM to the runner while the
        # job runs: the runner died, its trap graded the run at risk and it
        # paged. The job sleeps so the runner dies while it still waits.
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\n'
                                  f"JOB_COMMAND='{TERM_THE_RUNNER}; sleep 1'\n")
        run = self.fx.exec_job("demo")
        self.assertEqual(run.returncode, -signal.SIGTERM, run.stderr)
        log = (self.fx.folder / "logs" / "demo.log").read_text()
        self.assertRegex(log, r"(?m)^SD_REPORT_ATTENTION: data safe, one step failed: "
                              r"interrupted by a signal")
        self.assertRegex(log, r"FAILED rc=[0-9]+")
        self.assertEqual(self.pushes(), [])
        # The next run's real failure is still at risk, and pushes.
        self.job("exit 3")
        self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        self.assertEqual(len(self.pushes()), 1, self.pushes())

    def test_a_job_ended_by_a_signal_is_still_at_risk(self):
        # Only the runner's own end is an interruption: a workload that a
        # TERM ended is a failure like any other (ruling on sd:6030).
        self.job("kill -TERM \\$\\$")
        self.assertEqual(self.fx.exec_job("demo").returncode, 143)
        self.assertNotIn("data safe", (self.fx.folder / "logs" / "demo.log").read_text())
        self.assertEqual(len(self.pushes()), 1, self.pushes())

    def test_a_failure_run_under_the_launchctl_stubs_pushes_once(self):
        agents = self.fx.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "local.system-tools.cron.demo.plist").write_text("<plist/>\n")
        run = subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"],
            env=stub_env(self.fx, stub_bin(self.fx.tmp), 2),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assert_recorded_and_no_other_post()
        self.assertEqual(len(self.pushes()), 1, self.pushes())


class RealNotifierPushTest(unittest.TestCase):
    """The marker stands for a push the phone got: ntfy delivered (sd:5997).

    The real notify.sh runs here, beside the copy, with recording `curl`
    and `osascript` stubs. A banner that showed while ntfy failed or had no
    topic is not a push: the next failing run must try again.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="exit 3"\n')
        notify = self.fx.tmp / "local-notify" / "notify.sh"
        notify.parent.mkdir()
        shutil.copy(FOLDER.parent / "local-notify" / "notify.sh", notify)
        self.curl_log = self.fx.tmp / "curl-calls.txt"
        curl = self.fx.quiet / "curl"
        curl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{self.curl_log}'\n"
                        'exit "${CURL_EXIT:-0}"\n')
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
        self.marker = self.fx.folder / "logs" / ".demo.pushed"

    def topic(self):
        (self.fx.config / "notify").mkdir()
        (self.fx.config / "notify" / ".env").write_text("NTFY_TOPIC=example-topic\n")

    def posts(self):
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def test_a_banner_while_ntfy_fails_is_not_a_push(self):
        self.topic()
        for _ in range(2):
            run = self.fx.exec_job("demo", {"CURL_EXIT": "22"})
            self.assertEqual(run.returncode, 3)
            self.assertIn("at-risk push did not go out", run.stderr)
            self.assertFalse(self.marker.exists())
        self.assertEqual(len(self.posts()), 2, self.posts())

    def test_no_ntfy_topic_is_not_a_push(self):
        for _ in range(2):
            run = self.fx.exec_job("demo")
            self.assertEqual(run.returncode, 3)
            self.assertIn("at-risk push did not go out", run.stderr)
            self.assertFalse(self.marker.exists())
        self.assertEqual(self.posts(), [])

    def test_a_delivered_push_is_sent_once(self):
        self.topic()
        for _ in range(2):
            self.assertEqual(self.fx.exec_job("demo").returncode, 3)
        self.assertTrue(self.marker.exists())
        self.assertEqual(len(self.posts()), 1, self.posts())
        self.assertIn("example-topic", self.posts()[0])


def host_name():
    """`hostname -s`, as the script spells the host."""
    return subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip()


class WatchdogTest(unittest.TestCase):
    """`watchdog` names a job that stopped running to its own report.

    sd:3454: a stale job prints an `SD_REPORT_ATTENTION:` line, which the
    run's ingest maps to a report that needs attention, so Reports and Today
    carry it. It mails nothing: a notify.sh beside the fixture records any
    call, and must record none.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        notify = self.fx.tmp / "local-notify" / "notify.sh"
        notify.parent.mkdir()
        self.notify_log = self.fx.tmp / "notify-calls.txt"
        notify.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{self.notify_log}'\n")
        self.agents = self.fx.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)

    def install(self, job, installed_ago):
        self.fx.write_job(job, 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')
        plist = self.agents / f"local.system-tools.cron.{job}.plist"
        plist.write_text("<plist/>\n")
        then = time.time() - installed_ago
        os.utime(plist, (then, then))

    def watchdog(self, day="", now=""):
        # FAKE_DAY stands for the run's date, which the reason must not carry;
        # FAKE_NOW for the clock the ages are measured from.
        stub = self.fx.tmp / "day-bin" / "date"
        stub.parent.mkdir(exist_ok=True)
        stub.write_text('#!/bin/sh\n[ -n "$FAKE_DAY" ] && [ "$1" = "+%Y-%m-%d" ] && { echo "$FAKE_DAY"; exit 0; }\n'
                        '[ -n "$FAKE_NOW" ] && [ "$1" = "+%s" ] && { echo "$FAKE_NOW"; exit 0; }\n'
                        'exec /bin/date "$@"\n')
        stub.chmod(0o755)
        return subprocess.run(
            ["sh", str(self.fx.folder / "cron-jobs.sh"), "watchdog"],
            env={"PATH": self.fx.path(stub.parent), "HOME": str(self.fx.home),
                 "SYSTEM_TOOLS_CONFIG": str(self.fx.config), "FAKE_DAY": day, "FAKE_NOW": now},
            capture_output=True, text=True, timeout=60)

    def test_a_stale_job_needs_attention_and_mails_nothing(self):
        self.install("demo", 3 * 86400)
        run = self.watchdog()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("STALE   demo", run.stdout)
        self.assertRegex(run.stdout, r"(?m)^SD_REPORT_ATTENTION: watchdog on \S+: demo: never finished a run, "
                                     r"installed \d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertFalse(self.notify_log.exists(), self.notify_log.read_text() if self.notify_log.exists() else "")

    def test_a_job_within_its_cadence_needs_nothing(self):
        self.install("demo", 3600)
        run = self.watchdog()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("ok      demo", run.stdout)
        self.assertNotIn("SD_REPORT_ATTENTION", run.stdout)

    @staticmethod
    def stamp(ago):
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - ago))

    def test_a_lock_skipped_job_is_stale_though_its_log_is_fresh(self):
        """sd:3538: a skip line touches the log; only an outcome line counts."""
        self.install("demo", 5 * 86400)
        (self.fx.folder / "logs" / "demo.log").write_text(
            f"[demo] {self.stamp(3 * 86400)} done\n"
            f"[demo] {self.stamp(3600)} skipped: previous run still active\n")
        run = self.watchdog()
        self.assertIn("STALE   demo (last finished run 72h ago)", run.stdout)

    def test_a_failed_run_counts_as_a_run(self):
        self.install("demo", 5 * 86400)
        (self.fx.folder / "logs" / "demo.log").write_text(
            f"[demo] {self.stamp(3 * 86400)} done\n[demo] {self.stamp(3600)} FAILED rc=2\n")
        run = self.watchdog()
        self.assertIn("ok      demo", run.stdout)

    def test_a_log_without_an_outcome_measures_from_install(self):
        self.install("demo", 3 * 86400)
        (self.fx.folder / "logs" / "demo.log").write_text(
            f"[demo] {self.stamp(3600)} skipped: previous run still active\n")
        run = self.watchdog()
        self.assertIn("STALE   demo (never finished a run, installed 72h ago)", run.stdout)

    def test_the_reason_follows_the_stale_runs_not_the_day(self):
        """sd:4182: the ingest folds a run into the open report with its reason.
        A job still stale a night later gives the same reason though its age
        grew; a new stale state gives another (sd:3537: a checksum of job
        names alone made "never finished" and "cannot read its time" for one
        job one report)."""
        def reason(day, now=""):
            run = self.watchdog(day, now)
            return run.stdout, [l for l in run.stdout.splitlines() if l.startswith("SD_REPORT_ATTENTION: ")]
        self.install("demo", 3 * 86400)
        out, first = reason("2026-10-12")
        later_out, later = reason("2026-10-13", str(int(time.time()) + 86400))
        self.assertIn("STALE   demo (never finished a run, installed 72h ago)", out)
        self.assertIn("STALE   demo (never finished a run, installed 96h ago)", later_out)
        log = self.fx.folder / "logs" / "demo.log"
        log.write_text("[demo] 12 done\n")
        _, unreadable = reason("2026-10-13")
        log.write_text(f"[demo] {self.stamp(3 * 86400)} done\n")
        _, finished = reason("2026-10-13")
        log.write_text(f"[demo] {self.stamp(2 * 86400)} done\n")
        _, finished_again = reason("2026-10-13")
        self.assertEqual(len(first), 1, out)
        self.assertEqual(later, first)
        self.assertEqual(len({" ".join(r) for r in (first, unreadable, finished, finished_again)}), 4,
                         (first, unreadable, finished, finished_again))

    def test_an_unreadable_run_time_is_stale_not_fresh(self):
        self.install("demo", 3600)
        (self.fx.folder / "logs" / "demo.log").write_text("[demo] 12 done\n")
        run = self.watchdog()
        self.assertIn("STALE   demo (cannot read the time of its last run: 12)", run.stdout)
        self.assertRegex(run.stdout, r"(?m)^SD_REPORT_ATTENTION: watchdog on \S+: demo: cannot read 12$")

    def test_each_stale_job_is_its_own_line(self):
        """sd:4664: one line per stale job, so a job that runs again leaves a
        subset, which the pack folds into the open report (pack sd:4658)."""
        self.install("demo", 3 * 86400)
        self.install("other", 3 * 86400)
        both = [l for l in self.watchdog().stdout.splitlines() if l.startswith("SD_REPORT_ATTENTION: ")]
        (self.fx.folder / "logs" / "other.log").write_text(f"[other] {self.stamp(3600)} done\n")
        one = [l for l in self.watchdog().stdout.splitlines() if l.startswith("SD_REPORT_ATTENTION: ")]
        self.assertEqual(len(both), 2, both)
        self.assertEqual(one, [l for l in both if ": demo: " in l])


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

    def test_a_timeout_logs_the_process_tree_before_the_kill(self):
        """sd:5064: the log named the group only, so what hung was a guess.
        Each process in the group goes to the log, with its parent and age,
        before TERM."""
        self.reap("sleep 45.2361")
        self.job("'sleep 45.2361 & sleep 45.2361; wait'", timeout="1")
        self.assertEqual(self.run_job().returncode, 124)
        log = self.log.read_text().splitlines()
        head = next(i for i, line in enumerate(log) if "timed out after 1s" in line)
        group = re.search(r"process group (\d+)", log[head]).group(1)
        self.assertEqual(log[head + 1], "  PID PPID PGID ELAPSED COMMAND", log)
        tree = [line.split(None, 4) for line in log[head + 2:] if line.startswith("  ")]
        self.assertEqual(sorted(row[4] for row in tree if row[4] == "sleep 45.2361"),
                         ["sleep 45.2361", "sleep 45.2361"], log)
        self.assertTrue(all(row[2] == group for row in tree), log)

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


class TimeoutBreakerTest(unittest.TestCase):
    """Three timeouts in a row open a job's breaker (sd:6831).

    REGRESSION. A hung /Volumes/local held build-dispatch to its 90 s limit
    for 12.6 hours on 2026-10-27/28: every two-minute slot spawned a run
    that timed out, 383 rc=124 lines in failures.log. Now the third
    timeout in a row logs one `breaker open` line; later slots skip until
    a probe is due, and a probe's timeout adds no failures.log line.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.logs = self.fx.folder / "logs"
        self.ran = self.fx.tmp / "ran"
        self.state = self.logs / ".demo.timeouts"

    def job(self, rc):
        self.fx.write_job("demo", 'JOB_SCHEDULE="*/2 * * * *"\n'
                          f'JOB_COMMAND="echo x >> {self.ran}; exit {rc}"\n')

    def runs(self):
        return len(self.ran.read_text().splitlines()) if self.ran.exists() else 0

    def failures(self):
        path = self.logs / "failures.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_three_timeouts_open_the_breaker_and_later_slots_skip(self):
        self.job(124)
        for _ in range(3):
            self.assertEqual(self.fx.exec_job("demo").returncode, 124)
        result = self.fx.exec_job("demo")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipped: breaker open after 3 timeouts in a row", result.stderr)
        self.assertEqual(self.runs(), 3)
        failures = self.failures()
        self.assertEqual(len(failures), 4, failures)
        self.assertIn("demo breaker open: 3 timeouts in a row", failures[-1])

    def test_a_due_probe_runs_and_its_timeout_logs_no_new_failure(self):
        self.job(124)
        self.state.write_text(f"3 {int(time.time()) - 3600}\n")
        result = self.fx.exec_job("demo")
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.state.read_text().split()[0], "4")

    def test_a_probe_that_passes_closes_the_breaker(self):
        self.job(0)
        self.state.write_text(f"3 {int(time.time()) - 3600}\n")
        self.assertEqual(self.fx.exec_job("demo").returncode, 0)
        self.assertFalse(self.state.exists())

    def test_a_job_with_no_timeouts_logs_no_missing_state_file(self):
        # sd:6938: bash opens `< file` before `2>/dev/null`, so every
        # run without a state file printed "No such file or directory".
        for rc in (0, 124):
            self.state.unlink(missing_ok=True)
            self.job(rc)
            result = self.fx.exec_job("demo")
            self.assertNotIn(".demo.timeouts", result.stdout + result.stderr)

    def test_another_failure_resets_the_count(self):
        self.job(124)
        self.state.write_text(f"2 {int(time.time())}\n")
        self.job(1)
        self.assertEqual(self.fx.exec_job("demo").returncode, 1)
        self.assertFalse(self.state.exists())


class JobPathTest(unittest.TestCase):
    """The PATH a plist gives its job, built from this machine's Homebrew.

    The script probes the fixed prefix /opt/homebrew, so the copy under test
    names a fixture folder there instead, holding an executable `brew`.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.prefix = self.fx.tmp / "brew"
        (self.prefix / "bin").mkdir(parents=True)
        brew = self.prefix / "bin" / "brew"
        brew.write_text("#!/bin/sh\n")
        brew.chmod(brew.stat().st_mode | stat.S_IXUSR)
        script = self.fx.folder / "cron-jobs.sh"
        script.write_text(script.read_text().replace("/opt/homebrew", str(self.prefix)))
        self.fx.write_job("demo", 'JOB_SCHEDULE="0 3 * * *"\nJOB_COMMAND="true"\n')

    def job_path(self):
        env = {"PATH": self.fx.path(stub_bin(self.fx.tmp)), "HOME": str(self.fx.home),
               "SYSTEM_TOOLS_CONFIG": str(self.fx.config), "CRON_TEST_HELD": "0"}
        result = subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "install", "demo"],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.fx.home / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.cron.demo.plist"
        return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["PATH"].split(":")

    def test_rustup_proxies_come_before_brew_bin(self):
        """REGRESSION (pack sd:2881). brew bin can hold the rust formula's
        cargo, which ignores rust-toolchain.toml; a login shell finds the
        rustup proxy first, and the job has to find the same one."""
        rustup = self.prefix / "opt" / "rustup" / "bin"
        rustup.mkdir(parents=True)
        path = self.job_path()
        self.assertIn(str(rustup), path)
        self.assertEqual(path.index(str(rustup)) + 1, path.index(str(self.prefix / "bin")))

    def test_no_rustup_entry_without_the_folder(self):
        """PIN. A machine without Homebrew's rustup gets no entry that does not exist."""
        path = self.job_path()
        self.assertNotIn(str(self.prefix / "opt" / "rustup" / "bin"), path)
        self.assertIn(str(self.prefix / "bin"), path)


class UsageTest(unittest.TestCase):
    """The no-arg usage and the help print one usage line (sd:3541)."""

    def test_no_arg_usage_is_the_help_usage_and_names_every_verb(self):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        env = {"PATH": fx.path(), "HOME": str(fx.home), "SYSTEM_TOOLS_CONFIG": str(fx.config)}
        script = str(fx.folder / "cron-jobs.sh")
        bare = subprocess.run(["sh", script], env=env, capture_output=True, text=True, timeout=60)
        helped = subprocess.run(["sh", script, "help"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(bare.returncode, 1)
        self.assertEqual(bare.stderr.strip(), helped.stdout.splitlines()[0])
        for verb in ("verify", "exec <job>", "watchdog", "test"):
            self.assertIn(verb, bare.stderr)


class JobLaunchTest(unittest.TestCase):
    """sd:6456. launchd starts job-launch, which runs cron-jobs.sh as a child.

    REGRESSION. Under launchd Apple's /bin/bash passes its Full Disk Access
    only to Apple tools; Homebrew python under it was charged to itself and
    lost its grant on each upgrade. `install` builds the launcher once and
    never rebuilds it: a rebuild changes its code hash and drops its grant.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="manual"\nJOB_COMMAND="true"\n')
        self.plist = self.fx.home / "Library" / "LaunchAgents" / "local.system-tools.cron.demo.plist"
        self.launcher = self.fx.home / ".local" / "libexec" / "system-tools" / "job-launch"
        self.stubs = stub_bin(self.fx.tmp)
        self.launcher.unlink()
        self.cc_log = self.fx.tmp / "cc-calls"
        # A stub compiler: records its arguments and writes the -o file.
        (self.stubs / "cc").write_text(
            '#!/bin/sh\necho "$*" >> "$CC_LOG"\n'
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && { printf x > "$2"; chmod +x "$2"; }; shift; done\n')
        (self.stubs / "cc").chmod(0o755)

    def install(self):
        env = {"PATH": self.fx.path(self.stubs), "HOME": str(self.fx.home),
               "SYSTEM_TOOLS_CONFIG": str(self.fx.config), "CRON_TEST_HELD": "0", "CRON_TEST_RUNS": "0",
               "CRON_TEST_BOOTSTRAP_COUNT": str(self.fx.tmp / "bootstrap-count"), "CC_LOG": str(self.cc_log)}
        return subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), "install", "demo"],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_the_plist_starts_the_launcher_with_apple_bash_as_its_child(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = plistlib.loads(self.plist.read_bytes())["ProgramArguments"]
        self.assertEqual(args, [str(self.launcher), "/bin/bash",
                                str(self.fx.folder / "cron-jobs.sh"), "exec", "demo"])

    def test_install_builds_a_missing_launcher_and_names_the_grant(self):
        result = self.install()
        self.assertTrue(os.access(self.launcher, os.X_OK), result.stdout + result.stderr)
        self.assertIn(f"-o {self.launcher} {self.fx.folder / 'job-launch.c'}", self.cc_log.read_text())
        self.assertIn("grant it Full Disk Access", result.stdout)

    def test_install_never_rebuilds_a_launcher_that_exists(self):
        self.launcher.write_text("granted")
        self.launcher.chmod(0o755)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.cc_log.exists())
        self.assertEqual(self.launcher.read_text(), "granted")

    def test_a_failed_build_installs_nothing(self):
        (self.stubs / "cc").write_text("#!/bin/sh\nexit 1\n")
        result = self.install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cannot build", result.stderr)
        self.assertFalse(self.plist.exists())


class ManualScheduleTest(unittest.TestCase):
    """sd:4965. `JOB_SCHEDULE="manual"` installs a job that runs only when started.

    REGRESSION. The plist carries no StartCalendarInterval and RunAtLoad is
    false, so only `launchctl kickstart` (Management's Run now) starts it.
    `install`, `list` and `status` say "manual", and `watchdog` measures no
    cadence for it: a manual job has none, and its one-field schedule ended
    the sweep on an unbound `$3`. launchctl is the stub on PATH.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.fx.write_job("demo", 'JOB_SCHEDULE="manual"\nJOB_COMMAND="true"\n')
        self.plist = self.fx.home / "Library" / "LaunchAgents" / "local.system-tools.cron.demo.plist"

    def run_script(self, *args, env=None):
        base = {"PATH": self.fx.path(stub_bin(self.fx.tmp)), "HOME": str(self.fx.home),
                "SYSTEM_TOOLS_CONFIG": str(self.fx.config), "CRON_TEST_HELD": "0", "CRON_TEST_RUNS": "0",
                "CRON_TEST_BOOTSTRAP_COUNT": str(self.fx.tmp / "bootstrap-count")}
        base.update(env or {})
        return subprocess.run(["sh", str(self.fx.folder / "cron-jobs.sh"), *args],
                              env=base, capture_output=True, text=True, timeout=60)

    def test_install_writes_a_plist_with_no_calendar_and_no_run_at_load(self):
        result = self.run_script("install", "demo")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("installed: demo  (schedule: manual, runs only when started", result.stdout)
        config = plistlib.loads(self.plist.read_bytes())
        self.assertNotIn("StartCalendarInterval", config)
        self.assertIs(config["RunAtLoad"], False)
        self.assertEqual(config["ProgramArguments"][-2:], ["exec", "demo"])

    def test_a_scheduled_job_keeps_its_calendar(self):
        self.fx.write_job("demo", 'JOB_SCHEDULE="5 3 * * *"\nJOB_COMMAND="true"\n')
        self.assertEqual(self.run_script("install", "demo").returncode, 0)
        config = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(config["StartCalendarInterval"], [{"Minute": 5, "Hour": 3}])

    def test_list_and_status_say_manual(self):
        self.assertEqual(self.run_script("install", "demo").returncode, 0)
        listed = self.run_script("list")
        self.assertRegex(listed.stdout, r"(?m)^demo\s+manual\s")
        # Installed and never started: launchd holds the label at `runs = 0`.
        status = self.run_script("status", "demo", env={"CRON_TEST_HELD": "1"})
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("  schedule: manual; runs only when started (launchctl kickstart)", status.stdout)

    def test_watchdog_skips_a_manual_job(self):
        self.assertEqual(self.run_script("install", "demo").returncode, 0)
        then = time.time() - 30 * 86400
        os.utime(self.plist, (then, then))
        result = self.run_script("watchdog")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("manual  demo", result.stdout)
        self.assertNotIn("STALE", result.stdout)
        self.assertNotIn("SD_REPORT_ATTENTION", result.stdout)


if __name__ == "__main__":
    unittest.main()
