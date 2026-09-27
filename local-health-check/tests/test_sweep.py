"""The status sweep, run against a fixture tree of scratch tools.

`health-check.sh check` is pointed at a root of seven scratch folders through
`HEALTH_CHECK_TOOLS_ROOT`, with `HEALTH_CHECK_STATE` in a temp dir so the
real baselines are never touched, and `HEALTH_CHECK_STATUS_BOUND=2` so the
hang fixture costs two seconds and not thirty. The other stages -- crash
reports, launchd, SMART, log faults, network -- still run against the real
machine and may raise findings of their own, so every assertion here is
scoped to the lines that name a fixture folder or the fixture root.

One `check` run is shared by the cases (setUpClass): the fixture stage is
cheap, but the machine stages are not (`log show --last 24h` alone takes
tens of seconds on a lived-in Mac), and running them once per case would
multiply that by the number of cases for no extra evidence.

Python `unittest` and not sh for the reason CLAUDE.md gives for the sibling
suites: the CI wrapper asserts a unittest summary and refuses skips.
"""

import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "health-check.sh"
STATUS_BOUND = 2
# How long past the bound the hang fixture may be seen alive before the
# sweep is judged not to have enforced it. The stages after the hang in the
# sweep are a `help` probe and a `status` that exit at once.
BOUND_MARGIN = 5
# The outer guard: a `check` that does not return in this long means the
# bound did not fire at all (the fixture sleeps 600), and the process group
# is killed so the suite reports a failure instead of hanging with it.
RUN_TIMEOUT = 300
# How long the process group may take to empty after `check` returns.
SURVIVOR_GRACE = 2

PHRASE = "local-health-check reads these codes"


def _fixture(root, folder, stem, body):
    """Write `<root>/<folder>/<stem>.sh`, the convention-1 entrypoint."""
    d = root / folder
    d.mkdir()
    path = d / (stem + ".sh")
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class SweepAgainstFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="health-check-test."))
        cls.root = cls.tmp / "tools"
        cls.root.mkdir()
        cls.state = cls.tmp / "state"
        cls.state.mkdir()
        # A file every fixture appends its verb to, so "never run" is a fact
        # read from the fixture and not the absence of a finding.
        cls.calls = cls.tmp / "calls"

        cls.broken = _fixture(cls.root, "local-declares-broken", "declares-broken", f"""\
echo "declares-broken $1" >> "{cls.calls}"
case "${{1:-}}" in
  help)
    echo "usage: declares-broken.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status)
    echo "FAIL — the widget is on fire (HTTP 418)"
    echo "a second line the finding must not carry"
    exit 1 ;;
  *) echo "usage: declares-broken.sh status" >&2; exit 1 ;;
esac
""")
        cls.skip = _fixture(cls.root, "local-declares-skip", "declares-skip", f"""\
echo "declares-skip $1" >> "{cls.calls}"
case "${{1:-}}" in
  help)
    echo "usage: declares-skip.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) echo "SKIP — not configured on this machine"; exit 3 ;;
  *) echo "usage: declares-skip.sh status" >&2; exit 1 ;;
esac
""")
        # `status` notes when it started before it sleeps, so the bound can
        # be measured from the moment the hang began and not from the start
        # of a run whose other stages take however long the machine takes.
        cls.hang_started = cls.tmp / "hang-started"
        cls.hangs = _fixture(cls.root, "local-declares-hangs", "declares-hangs", f"""\
echo "declares-hangs $1" >> "{cls.calls}"
case "${{1:-}}" in
  help)
    echo "usage: declares-hangs.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) date +%s > "{cls.hang_started}"; sleep 600 ;;
  *) echo "usage: declares-hangs.sh status" >&2; exit 1 ;;
esac
""")
        # A `status` that leaves a grandchild behind: the `sleep` is a child
        # of an inner `sh`, not of the entrypoint, so only a walk of the whole
        # tree finds it when the bound expires (sd:1224).
        cls.deep = _fixture(cls.root, "local-declares-deep", "declares-deep", f"""\
echo "declares-deep $1" >> "{cls.calls}"
case "${{1:-}}" in
  help)
    echo "usage: declares-deep.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) sh -c 'sleep 600; :' ;;
  *) echo "usage: declares-deep.sh status" >&2; exit 1 ;;
esac
""")
        # A `status` that ignores TERM, and so does the `sleep` it starts: a
        # disposition set to ignore survives exec. Only KILL ends it, and a
        # bound that sends TERM alone waits for it for ten minutes (sd:1224).
        cls.deaf = _fixture(cls.root, "local-declares-deaf", "declares-deaf", f"""\
echo "declares-deaf $1" >> "{cls.calls}"
case "${{1:-}}" in
  help)
    echo "usage: declares-deaf.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) trap '' TERM; sleep 600 ;;
  *) echo "usage: declares-deaf.sh status" >&2; exit 1 ;;
esac
""")
        # A `status` that answers at once with 124, the code `timeout` uses.
        # It is a code convention 6 does not define, not a hang (sd:1224).
        cls.code124 = _fixture(cls.root, "local-declares-exit124", "declares-exit124", f"""\
echo "declares-exit124 $1" >> "{cls.calls}"
case "${{1:-}}" in
  help)
    echo "usage: declares-exit124.sh status"
    echo "  status  0 healthy, 3 nothing to check, 1 broken; {PHRASE}"
    exit 0 ;;
  status) echo "answered at once"; exit 124 ;;
  *) echo "usage: declares-exit124.sh status" >&2; exit 1 ;;
esac
""")
        # No phrase and no `status` verb: convention 1's usage-and-exit-1,
        # which is the finding code, and which the sweep must never see.
        cls.silent = _fixture(cls.root, "local-silent", "silent", f"""\
echo "silent $1" >> "{cls.calls}"
case "${{1:-}}" in
  help) echo "usage: silent.sh start|stop"; exit 0 ;;
  *) echo "usage: silent.sh start|stop" >&2; exit 1 ;;
esac
""")

        env = dict(os.environ)
        env["HEALTH_CHECK_TOOLS_ROOT"] = str(cls.root)
        env["HEALTH_CHECK_STATE"] = str(cls.state)
        env["HEALTH_CHECK_STATUS_BOUND"] = str(STATUS_BOUND)
        # The Jev ordering is on unless switched off, and this suite is about
        # the sweep and not about Jev: left on, it labels the findings this
        # file compares byte for byte, and it spends real tokens doing it.
        env["JEV_HEALTH_CHECK"] = "0"
        # Its own session, so the process group is the script's pid and
        # everything the run spawned can be found -- or killed -- by it.
        proc = subprocess.Popen(
            ["sh", str(SCRIPT), "check"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        cls.finished_at = time.time()
        cls.pgid = proc.pid
        cls.rc = proc.returncode
        cls.out = out
        cls.err = err

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def findings(self):
        """Every finding as (headline, fix), from the `findings:` block."""
        lines = self.out.splitlines()
        if "findings:" not in lines:
            return []
        start = lines.index("findings:") + 1
        end = lines.index("notes:") if "notes:" in lines else len(lines)
        block = [l for l in lines[start:end] if l.strip()]
        pairs = []
        for i in range(0, len(block), 2):
            head = block[i]
            fix = block[i + 1] if i + 1 < len(block) else ""
            self.assertTrue(head.startswith("- "), head)
            self.assertTrue(fix.startswith("  fix: "), fix)
            pairs.append((head[2:], fix[len("  fix: "):]))
        return pairs

    def sweep_findings(self):
        """The findings that name a fixture folder or the fixture root.

        Findings from the machine stages are not this suite's business and
        cannot name a scratch folder that did not exist a second ago.
        """
        root = str(self.root)
        return [
            (h, f) for h, f in self.findings()
            if root in h or root in f or h.startswith("local-declares-")
            or h.startswith("local-silent")
        ]

    def calls_made(self):
        try:
            return self.calls.read_text().splitlines()
        except FileNotFoundError:
            return []

    # -- cases -------------------------------------------------------------

    def test_check_ran_and_reported(self):
        self.assertEqual(self.rc, 0, self.err)
        self.assertIn("health check — ", self.out)
        self.assertIn("findings:", self.out, self.out)

    def test_exactly_five_findings_name_the_fixtures(self):
        names = sorted(h.split(":")[0] for h, _ in self.sweep_findings())
        self.assertEqual(names, ["local-declares-broken", "local-declares-deaf",
                                 "local-declares-deep", "local-declares-exit124",
                                 "local-declares-hangs"],
                         self.out)

    def test_broken_status_is_a_finding_carrying_its_first_line(self):
        [(head, fix)] = [(h, f) for h, f in self.sweep_findings()
                         if h.startswith("local-declares-broken")]
        self.assertEqual(
            head, "local-declares-broken: FAIL — the widget is on fire (HTTP 418)")
        self.assertNotIn("a second line", head)
        self.assertEqual(fix, f"run {self.broken} status by hand for the full response")
        # The remedy is the tool's line, not another tool's: nothing about
        # service keys or pipeline ids reaches a scratch tool's finding.
        self.assertNotIn("service key", fix)
        self.assertNotIn("pipeline", fix)

    def test_hang_is_a_finding_naming_the_tool_and_the_bound(self):
        [(head, fix)] = [(h, f) for h, f in self.sweep_findings()
                         if h.startswith("local-declares-hangs")]
        self.assertEqual(
            head, f"local-declares-hangs: status did not answer in {STATUS_BOUND}s")
        self.assertIn(f"run {self.hangs} status by hand", fix)

    def test_every_hang_is_reported_as_a_hang(self):
        for folder in ("local-declares-deep", "local-declares-deaf"):
            with self.subTest(folder=folder):
                heads = [h for h, _ in self.sweep_findings()
                         if h.startswith(folder + ":")]
                self.assertEqual(
                    heads, [f"{folder}: status did not answer in {STATUS_BOUND}s"],
                    self.out)

    def test_a_status_that_exits_124_is_not_a_hang(self):
        [(head, fix)] = [(h, f) for h, f in self.sweep_findings()
                         if h.startswith("local-declares-exit124")]
        self.assertEqual(
            head, "local-declares-exit124: status exited 124, which convention 6 "
                  "does not define (0 healthy, 3 nothing to check, 1 broken)"
                  " — answered at once")
        self.assertIn(f"run {self.code124} status by hand", fix)

    def test_skip_exit_is_silent(self):
        self.assertNotIn("local-declares-skip", self.out)
        # It was declared and asked, though: silence is an answer, not an
        # omission.
        self.assertIn("declares-skip status", self.calls_made())

    def test_silent_entrypoint_is_never_asked_for_status(self):
        self.assertNotIn("local-silent", self.out)
        self.assertNotIn("usage:", self.out)
        calls = [c for c in self.calls_made() if c.startswith("silent ")]
        self.assertEqual(calls, ["silent help"])

    def test_notes_count_declared_and_checked(self):
        # Six fixtures say the phrase; the three hangs are declared but never
        # answered, so they are not counted as checked.
        self.assertIn("- status sweep: 6 tool(s) declared, 3 checked",
                      self.out.splitlines())

    def test_bound_is_enforced_from_the_moment_the_hang_began(self):
        started = int(self.hang_started.read_text().strip())
        waited = self.finished_at - started
        self.assertLess(waited, STATUS_BOUND + BOUND_MARGIN,
                        f"check ran {waited:.1f}s past the hang fixture's start")

    def test_nothing_the_run_spawned_survives_it(self):
        # The hang fixture's `sleep 600` is a child of the killed `sh`; the
        # bound kills it too. Anything still in the run's process group --
        # that sleep, a watchdog, a stray subshell -- is a leak.
        #
        # The group is read for up to SURVIVOR_GRACE seconds, because a
        # signal is delivered and a dead orphan reaped by launchd after the
        # `kill` returns, not during it. That grace cannot hide a leak: the
        # one this test caught (sd:1418) was a `sleep 600` nobody signalled,
        # and it was still there ten minutes later.
        deadline = time.monotonic() + SURVIVOR_GRACE
        while True:
            ps = subprocess.run(
                ["ps", "-A", "-o", "pgid=,pid=,command="],
                stdout=subprocess.PIPE, text=True, check=True,
            ).stdout
            survivors = [
                line for line in ps.splitlines()
                if line.split() and line.split()[0] == str(self.pgid)
            ]
            if not survivors or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        self.assertEqual(survivors, [])

    def test_state_stayed_in_the_temp_dir(self):
        # `check` seeds both baselines on a first run, and it seeded them
        # where it was told to and not in ~/.config/health-check.
        self.assertTrue((self.state / "fault-count").exists())
        self.assertTrue((self.state / "launchd-baseline").exists())
        self.assertIn("baseline seeded", self.out)


class ReadmeCountsByTheProbe(unittest.TestCase):
    """The README's "who is in" answer is the probe, never a source grep.

    `grep -l 'local-health-check' */*.sh` lists this folder's own script,
    which the sweep skips, and any script that merely mentions the sweep;
    and from this folder, where the README runs `./health-check.sh check`,
    `*/*.sh` matches nothing at all (sd:1224).
    """

    def test_no_source_grep_is_offered_as_the_membership(self):
        text = (HERE.parent / "README.md").read_text()
        self.assertNotIn("grep -l 'local-health-check'", text)
        self.assertIn("status sweep: N tool(s) declared, M checked", text)


class StatusBoundIsValidated(unittest.TestCase):
    """HEALTH_CHECK_STATUS_BOUND may shorten the bound, never break it.

    A value `bounded` cannot use is refused with exit 1 before the machine
    stages run, so a refusal costs well under a second. Whether the script
    got past the check is read from the state directory, which is created
    on the line after it: a refused value leaves it absent, and an accepted
    one creates it at once, after which the run is killed rather than left
    to spend minutes on the machine stages.
    """

    REFUSE_TIMEOUT = 20

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="health-check-bound."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "tools").mkdir()
        (self.tmp / "tmp").mkdir()

    def start(self, bound):
        self.runs = getattr(self, "runs", 0) + 1
        state = self.tmp / f"state-{self.runs}"
        env = dict(os.environ)
        env["HEALTH_CHECK_TOOLS_ROOT"] = str(self.tmp / "tools")
        env["HEALTH_CHECK_STATE"] = str(state)
        env["HEALTH_CHECK_STATUS_BOUND"] = bound
        # mktemp -d lands here, so a run killed below leaves nothing behind.
        env["TMPDIR"] = str(self.tmp / "tmp")
        env["JEV_HEALTH_CHECK"] = "0"
        proc = subprocess.Popen(
            ["sh", str(SCRIPT), "check"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, start_new_session=True,
        )
        return proc, state

    def test_values_bounded_cannot_use_are_refused(self):
        for bad in ["0", "00", "-1", "31", "600", "abc", "1.5", "5m", " 5",
                    "05", "2x"]:
            with self.subTest(bound=bad):
                proc, state = self.start(bad)
                try:
                    out, err = proc.communicate(timeout=self.REFUSE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()
                    self.fail(f"HEALTH_CHECK_STATUS_BOUND={bad!r} was not "
                              f"refused within {self.REFUSE_TIMEOUT}s")
                self.assertEqual(proc.returncode, 1, out + err)
                self.assertEqual(
                    err,
                    "health-check.sh: HEALTH_CHECK_STATUS_BOUND must be whole "
                    f"seconds from 1 to 30, got '{bad}'\n")
                self.assertEqual(out, "")
                self.assertFalse(state.exists(),
                                 "the run went past the bound check")

    def test_whole_seconds_from_1_to_30_are_accepted(self):
        for good in ["1", "9", "10", "29", "30", ""]:
            with self.subTest(bound=good):
                proc, state = self.start(good)
                try:
                    deadline = time.monotonic() + self.REFUSE_TIMEOUT
                    while not state.exists() and proc.poll() is None \
                            and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(state.exists(),
                                    proc.stderr.read() if proc.poll() is not None
                                    else "state dir never created")
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.communicate()


if __name__ == "__main__":
    unittest.main()
