"""lib/bounded.sh: a step that hangs ends at its bound and names itself (sd:2660)."""

import os
import re
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
CRON_JOBS = LIB.parent / "local-cron-jobs" / "cron-jobs.sh"


def cron_jobs_grace():
    """cron-jobs' default grace between a job's TERM and its KILL, read from
    the script itself so the two defaults cannot drift apart unseen."""
    found = re.findall(r'"\$\{CRON_JOBS_TIMEOUT_GRACE:-(\d+)\}"', CRON_JOBS.read_text())
    assert len(set(found)) == 1, found
    return int(found[0])


def shell(script, **kwargs):
    return subprocess.run(["/bin/sh", "-c", f'. "{LIB}/bounded.sh"; {script}'],
                          capture_output=True, text=True, check=False, timeout=60, **kwargs)


def gone(pid, within=5.0):
    """True once no process has `pid`, waiting up to `within` seconds."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def kill_group(pgid):
    """Cleanup: KILL a group a failed case may have left; gone is fine."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Bounded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_command_in_time_keeps_its_output_and_code(self):
        result = shell("st_bounded 10 sh -c 'echo out; echo err >&2; exit 3'")

        self.assertEqual(result.returncode, 3)
        self.assertEqual((result.stdout, result.stderr), ("out\n", "err\n"))

    def test_an_expired_bound_exits_124_and_names_the_command(self):
        started = time.monotonic()
        result = shell("st_bounded 1 sleep 30")

        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stderr, "timed out after 1s: sleep 30\n")
        self.assertLess(time.monotonic() - started, 10)

    def test_a_command_that_ignores_term_is_killed_after_the_grace(self):
        started = time.monotonic()
        result = shell("ST_BOUNDED_GRACE=1 st_bounded 1 sh -c 'trap \"\" TERM; sleep 30'")

        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started, 10)

    def test_the_bound_ends_the_command_s_children_too(self):
        pidfile = self.dir / "child.pid"
        started = time.monotonic()
        result = shell(f"st_bounded 1 sh -c 'sleep 30 & echo $! > \"{pidfile}\"; wait'")

        self.assertEqual(result.returncode, 124)
        self.assertTrue(gone(int(pidfile.read_text())), "the child outlived the bound")
        # Ended by the TERM, not by the KILL ten seconds later.
        self.assertLess(time.monotonic() - started, 8)

    def test_a_term_to_the_caller_reaches_the_command(self):
        """The job's own limit TERMs the job's group; the bounded command is in
        a group of its own, so the TERM has to be passed on."""
        pidfile = self.dir / "child.pid"
        process = subprocess.Popen(
            ["/bin/sh", "-c", f'. "{LIB}/bounded.sh"; '
                              f"st_bounded 60 sh -c 'echo $$ > \"{pidfile}\"; exec sleep 60'"],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        deadline = time.monotonic() + 10
        while not pidfile.exists() or not pidfile.read_text().strip():
            self.assertLess(time.monotonic(), deadline, "the command never started")
            time.sleep(0.05)
        child = int(pidfile.read_text())
        self.assertNotEqual(os.getpgid(child), process.pid)

        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=20)

        self.assertTrue(gone(child), "the bounded command outlived the TERM")

    def test_a_command_that_ignores_a_term_to_the_caller_is_killed_after_the_grace(self):
        """cron-jobs TERMs the job's group, then KILLs it after its own grace.
        The KILL cannot reach a command in a group of its own, so the bound
        has to end it within its grace and exit, or the command outlives the
        job and overlaps the next run."""
        self.term_a_command_that_ignores_it(grace="1", within=4)

    def test_the_default_grace_ends_the_command_well_inside_cron_jobs_grace(self):
        """With neither grace set, the whole stop -- TERM, grace, KILL, exit --
        ends at least two seconds before cron-jobs' KILL would arrive."""
        self.term_a_command_that_ignores_it(grace=None, within=cron_jobs_grace() - 2)

    def test_a_term_during_the_bound_s_own_stop_does_not_extend_it(self):
        """The timeout path: the bound expired and its stop is under way when
        cron-jobs' TERM arrives. The stop finishes on its own clock, still
        well inside cron-jobs' grace."""
        self.term_a_command_that_ignores_it(grace=None, within=cron_jobs_grace() - 2,
                                            bound=1, delay=1.5)

    def term_a_command_that_ignores_it(self, grace, within, bound=60, delay=0.0):
        pidfile = self.dir / "child.pid"
        env = {k: v for k, v in os.environ.items() if k != "ST_BOUNDED_GRACE"}
        if grace is not None:
            env["ST_BOUNDED_GRACE"] = grace
        process = subprocess.Popen(
            ["/bin/sh", "-c", f'. "{LIB}/bounded.sh"; st_bounded {bound} '
                              f"sh -c 'trap \"\" TERM; echo $$ $PPID > \"{pidfile}\"; exec sleep 60'"],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        deadline = time.monotonic() + 10
        while not pidfile.exists() or len(pidfile.read_text().split()) < 2:
            self.assertLess(time.monotonic(), deadline, "the command never started")
            time.sleep(0.05)
        child, supervisor = (int(word) for word in pidfile.read_text().split())
        self.addCleanup(kill_group, child)
        time.sleep(delay)

        os.killpg(process.pid, signal.SIGTERM)
        termed = time.monotonic()

        self.assertTrue(gone(child, within), "a command ignoring TERM outlived the grace")
        self.assertTrue(gone(supervisor, max(0.0, termed + within - time.monotonic()) + 0.2),
                        "the bound did not exit inside the grace")
        process.communicate(timeout=10)


class Step(unittest.TestCase):
    def test_the_step_is_logged_before_it_starts(self):
        result = shell("st_step 10 echo running 2>&1")

        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 0)
        self.assertRegex(lines[0], r"^\[step \d\d:\d\d:\d\d\] echo running \(bound 10s\)$")
        self.assertEqual(lines[1:], ["running"])

    def test_fd3_takes_the_start_line_and_a_failure_goes_to_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            result = shell(f'exec 3>"{log}"; ST_STEP_FD3=1; st_step 1 sleep 30')
            logged = log.read_text().splitlines()

        self.assertEqual(result.returncode, 124)
        self.assertEqual(len(logged), 2, logged)
        self.assertRegex(logged[0], r"\] sleep 30 \(bound 1s\)$")
        self.assertRegex(logged[1], r"\] sleep 30 exited 124$")
        self.assertEqual(result.stderr.splitlines()[0], "timed out after 1s: sleep 30")
        self.assertRegex(result.stderr.splitlines()[1], r"\] sleep 30 exited 124$")


# --- every hang-prone call a cron job runs is bounded (sd:3536) -----------
# sd:2660 bounded the brew and mas calls of one script, and the same hang
# stayed in the others. This reads the code a cron job runs and fails on each
# call in command position, the place a bare call stands; a bounded one is an
# argument to st_step, st_bounded or a wrapper of them, so it never is.

#: The scripts a cron job runs: each one a catalogue job names, and
#: machine-setup, which the follow job, the weekly upgrade-report and a
#: nightly `update --apply` run.
def cron_scripts():
    root = LIB.parent
    found = {root / "local-machine-setup" / "machine-setup.sh"}
    for job in sorted((root / "local-cron-jobs" / "examples").glob("*.job")):
        for name in re.findall(r'\$ROOT/\.\./([\w.-]+/[\w.-]+\.sh)', job.read_text()):
            found.add(root / name)
    return sorted(found)


#: Where a command starts: a line, a pipe or list, a substitution or a group;
#: then any keywords, negations, VAR=value prefixes and runners that run their
#: arguments as they are, machine-setup's `run` helpers among them.
START = (r"(?:^|[;&|(`{]|\$\()\s*(?:(?:!|if|then|do|else|elif|while|until|sudo|env|exec|eval"
         r"|command|run|run_ok|try_run)\s+|\w+=(?:\"\"|''|[^\s$`(]*)\s+)*")
#: A hang-prone call: brew, mas, softwareupdate, curl, or git reaching a remote.
#: A word followed by `|` or `)` is a case pattern, not a call.
CALL = re.compile(START + r"(?:[^\s;&|()]*/)?(?P<call>(?:brew|mas|softwareupdate|curl)(?=[\s;]|$)"
                  r"|git(?:\s+-[Cc]\s+\S+|\s+-\w+)*\s+(?:fetch|pull|push|clone|ls-remote"
                  r"|submodule\s+update|remote\s+update)\b)")
#: curl carries its own bound in its own command: a positive number, or a
#: variable (blanked to "" when quoted).
CURL_BOUND = re.compile(r"^[^;&|)]*\s(?:-m\s+|--max-time[\s=]+)(?:0*[1-9]|[\"$])")


def code_lines(text):
    """(line number, code) for each logical line of a shell script, read the
    way sh reads it: continuations joined, comments and here-document bodies
    dropped, and quoted text blanked, so a message that names a call is not
    one. Kept: what a double-quoted $(...) or `...` runs, and a
    single-quoted `-c` script, its newlines read as `;`. Not seen: a call in
    an unquoted here-document's substitution, or a quoted command name."""
    out, buf, number, line = [], [], 1, 1
    i, n, heredocs = 0, len(text), []

    def flush():
        code = "".join(buf)
        if code.strip():
            out.append((number, code))
        buf.clear()

    def substitution(i):
        """Code of a $( at i-2 up to its ), and the index after it."""
        depth, code = 1, []
        while i < n and depth:
            c = text[i]
            if c == "\\":
                code.append(text[i:i + 2]); i += 2; continue
            if c == "'":
                end = text.find("'", i + 1)
                end = n if end < 0 else end
                i = end + 1; code.append("''"); continue
            if c == '"':
                inner, i = double(i + 1)
                code.append(inner); continue
            depth += c == "("
            depth -= c == ")"
            if depth:
                code.append(" " if c == "\n" else c)
            i += 1
        return "$(" + "".join(code) + ")", i

    def double(i):
        """A double-quoted string from i: only its substitutions survive."""
        kept = []
        while i < n and text[i] != '"':
            if text[i] == "\\":
                i += 2; continue
            if text.startswith("$(", i):
                code, i = substitution(i + 2)
                kept.append(code); continue
            if text[i] == "`":
                end = text.find("`", i + 1)
                end = n - 1 if end < 0 else end
                kept.append(text[i:end + 1]); i = end + 1; continue
            i += 1
        return '"' + " ".join(kept) + '"', i + 1

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            if heredocs:
                while heredocs:
                    end = text.find("\n", i + 1)
                    end = n if end < 0 else end
                    body = text[i + 1:end]
                    line += 1
                    i = end
                    if body.strip() == heredocs[0]:
                        heredocs.pop(0)
            flush(); number = line; i += 1; continue
        if c == "\\" and text.startswith("\\\n", i):
            line += 1; buf.append(" "); i += 2; continue
        if c == "\\":
            buf.append(text[i:i + 2]); i += 2; continue
        if c == "#" and (not buf or buf[-1] in " \t;"):
            end = text.find("\n", i)
            i = n if end < 0 else end; continue
        if c == "'":
            end = text.find("'", i + 1)
            end = n if end < 0 else end
            body = text[i + 1:end]
            line += body.count("\n")
            script = re.search(r"(?:^|\s)-c\s*$", "".join(buf[-8:]))
            buf.append("';" + body.replace("\n", ";") + "'" if script else "''")
            i = end + 1; continue
        if c == '"':
            start = i
            kept, i = double(i + 1)
            line += text.count("\n", start, i)
            buf.append(kept); continue
        if text.startswith("$(", i):
            start = i
            code, i = substitution(i + 2)
            line += text.count("\n", start, i)
            buf.append(code); continue
        found = re.match(r"<<-?\s*(['\"]?)(\w+)\1", text[i:])
        if found and not text.startswith("<<<", i):
            heredocs.append(found.group(2))
            buf.append(found.group(0)); i += found.end(); continue
        buf.append(c); i += 1
    flush()
    return out


def unbounded_calls(path, name):
    """'name:line: code' for each unbounded call in the script at path."""
    found = []
    for number, line in code_lines(path.read_text()):
        for match in CALL.finditer(line):
            if match.group("call") == "curl" and CURL_BOUND.search(line[match.end():]):
                continue
            found.append(f"{name}:{number}: {line.strip()}")
    return found


class CronCallsAreBounded(unittest.TestCase):
    def test_the_scan_sees_a_bare_call(self):
        """The scan is only as good as what it can see: each shape fails."""
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "job.sh"
            script.write_text("\n".join([
                "brew upgrade",
                'x=$(mas outdated 2>/dev/null)',
                'if softwareupdate -l > "$f" 2>&1; then :; fi',
                'git -C "$d" fetch -q origin',
                '(cd "$d" && git pull --ff-only && git submodule update --init)',
                '  run brew install "$f"',
                'curl -fsSL \\',
                '  https://example.test/x',
                'eval "$(/opt/homebrew/bin/brew shellenv)"',
                "command brew upgrade",
                "sh -c 'brew upgrade'",
                'x="`mas outdated`"',
                "curl https://example.test; echo --max-time 10",
                "curl --max-time 0 https://example.test",
            ]) + "\n")
            found = [line.split(":")[1] for line in unbounded_calls(script, "job.sh")]
        self.assertEqual(found, ["1", "2", "3", "4", "5", "5", "6", "7", "9", "10", "11", "12", "13", "14"])

    def test_the_scan_passes_a_bounded_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "job.sh"
            script.write_text("\n".join([
                "st_step 1800 brew upgrade",
                'x=$(st_step 120 mas outdated 2>/dev/null)',
                'if st_bounded 300 git -C "$d" fetch -q origin; then :; fi',
                '  run bound_step 1800 brew install "$f"',
                'curl -s -m 10 https://example.test/x',
                'command -v brew >/dev/null 2>&1',
                'echo "  brew MISSING (brew install mas)"',
                "# brew upgrade in a comment",
                'git -C "$d" rev-parse HEAD',
            ]) + "\n")
            self.assertEqual(unbounded_calls(script, "job.sh"), [])

    def test_no_cron_script_makes_an_unbounded_call(self):
        scripts = cron_scripts()
        for name in ("local-maintenance/maintenance.sh", "local-repo-sync/repo-sync.sh",
                     "local-health-check/health-check.sh", "local-machine-setup/machine-setup.sh"):
            self.assertIn(LIB.parent / name, scripts)
        found = [line for path in scripts
                 for line in unbounded_calls(path, path.relative_to(LIB.parent))]
        self.assertEqual(found, [], "\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
