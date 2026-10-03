"""sd:760. A tile stops its own nested commands before its caller's kill.

Every command `collectors.run` starts is in a session of its own, which a kill
of the tile's process group does not reach. So when a caller stopped a tile at
its five seconds, the command in flight lived on with nobody to enforce its
timeout, and a vault probe held by TCC lost its Full Disk Access reason to a
bare "ran past its budget". `sd_tile.main` now sets a deadline, `TILE_SECONDS`
less `TILE_MARGIN`, and every nested timeout is capped at the time left.

Both callers are driven for real: the pack's loader, as a parent shaped like
`plugins.bounded_run` around `dashboard.sh tile`, and this dashboard's
resource views through `reports_screen.collect`. Neither can be scaled -- the
child is a real process -- so these run for the real four seconds.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sd_tile
from sd_dashboard import reports_screen

HERE = Path(__file__).resolve().parents[1]

# A command that records its pid, starts a grandchild in its own group that
# records its pid too, and then waits far past any budget here.
LAUNCHCTL = ('#!/bin/sh\necho $$ >> "$DEADLINE_PIDS"\nsleep 60 &\n'
             'echo $! >> "$DEADLINE_PIDS"\nwait\n')

# The vault probe as a TCC prompt nobody answers leaves it: no answer at all,
# for as long as anyone waits. Same shape as above, in Python.
HUNG_PROBE = ("import os, subprocess, sys, time\n"
              "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "open(os.environ['DEADLINE_PIDS'], 'a').write(f'{os.getpid()}\\n{grandchild.pid}\\n')\n"
              "time.sleep(60)\n")

# The real `sd_tile.py`, with only the probe's script replaced: the deadline,
# `vault_blocked`, `require_vault` and the tab builders are the checkout's.
# It records when it reaches `main`, on the machine-wide monotonic clock, so a
# test can tell a tile that started late from a cap that did not hold.
PROBE_TILE = (f"import os, sys, time\nsys.path.insert(0, {str(HERE)!r})\n"
              "import sd_tile\n"
              "load = sd_tile.load_collectors\n"
              "def hung():\n"
              "    module = load()\n"
              f"    module.VAULT_PROBE = {HUNG_PROBE!r}\n"
              "    return module\n"
              "sd_tile.load_collectors = hung\n"
              "open(os.environ['DEADLINE_MAIN'], 'w').write(repr(time.monotonic()))\n"
              "sys.exit(sd_tile.main(sys.argv[1:]))\n")

# The interpreter `dashboard.sh tile` execs, as `DASHBOARD_PYTHON`: it runs the
# copied checkout's own `sd_tile.main`, and records when it reaches it, as
# `PROBE_TILE` does, so the loader's test can tell a late start too.
RECORDING_PYTHON = (f"#!{sys.executable}\nimport os, sys, time\n"
                    "sys.argv = sys.argv[1:]\n"
                    "sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))\n"
                    "import sd_tile\n"
                    "open(os.environ['DEADLINE_MAIN'], 'w').write(repr(time.monotonic()))\n"
                    "sys.exit(sd_tile.main(sys.argv[1:]))\n")

# How long a check waits for the nested pids to be written, then to vanish. It
# returns as soon as they do; a survivor sleeps for a minute, so a bound below
# that still tells one apart. Five seconds did not hold at load 100 (sd:2333).
SURVIVE_SECONDS = 30


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie has stopped; `ps` says so where kill(0) cannot.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


class Nested(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tile-deadline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pids = self.root / "pids"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "launchctl").write_text(LAUNCHCTL)
        (self.bin / "launchctl").chmod(0o755)
        self.addCleanup(self.reap)

    def reap(self):
        # Whatever a failing run left behind, by pid rather than by name.
        for pid in self.recorded():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def recorded(self):
        # Whole lines only: a writer caught mid-line has not recorded that pid yet.
        text = self.pids.read_text() if self.pids.exists() else ""
        return [int(line) for line in text.splitlines(keepends=True) if line.endswith("\n")]

    def assert_none_survive(self, refusal):
        # `refusal` is what the caller was told, so a failure here still says
        # why the tile stopped (sd:763): a start that stalled past the caller's
        # kill reads differently from a cap that did not hold.
        why = f"; the caller was told: {refusal}"
        # Bounded, and generous for a loaded machine: a probe the tile started
        # late may write its pids after the caller returned. One that is still
        # running then is exactly what the check below is for.
        deadline = time.monotonic() + SURVIVE_SECONDS
        pids = self.recorded()
        while len(pids) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            pids = self.recorded()
        self.assertEqual(len(pids), 2, "the nested command and its grandchild did not both start" + why)
        # SIGKILL is prompt, and a survivor sleeps for a minute.
        deadline = time.monotonic() + SURVIVE_SECONDS
        while pids and time.monotonic() < deadline:
            pids = [pid for pid in pids if running(pid)]
            time.sleep(0.05)
        self.assertEqual(pids, [], "a nested command outlived the tile" + why)

    def environment(self):
        return {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "DEADLINE_PIDS": str(self.pids)}

    def test_the_loader_gets_the_tiles_reason_before_its_own_kill(self):
        # The pack's path: `dashboard.sh tile toolbox` from a checkout-shaped
        # copy, under a parent that starts its clock when `Popen` returns and
        # kills the process group at `TILE_SECONDS`, as `bounded_run` does.
        # `launchctl list` declares 25 seconds of its own.
        dashboard = self.root / "checkout" / "local-project-dashboard"
        dashboard.mkdir(parents=True)
        for name in ("dashboard.sh", "sd_tile.py", "collectors.py"):
            shutil.copy2(HERE / name, dashboard / name)
        # dashboard.sh and collectors.py read the checkout's shared config helpers.
        shutil.copytree(HERE.parent / "lib", dashboard.parent / "lib")
        python = self.root / "python"
        python.write_text(RECORDING_PYTHON)
        python.chmod(0o755)
        reached = self.root / "main"
        environment = {**self.environment(), "HOME": str(self.root), "DASHBOARD_PYTHON": str(python),
                       "DEADLINE_MAIN": str(reached)}
        for attempt in (1, 2, 3):
            self.reap()
            self.pids.unlink(missing_ok=True)
            reached.unlink(missing_ok=True)
            process = subprocess.Popen(["./local-project-dashboard/dashboard.sh", "tile", "toolbox"],
                cwd=dashboard.parent, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
            started = time.monotonic()
            stop = started + sd_tile.TILE_SECONDS
            try:
                out, err = process.communicate(timeout=stop - time.monotonic())
                killed = False
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                out, err = process.communicate()
                killed = True
            elapsed = time.monotonic() - started
            late = float(reached.read_text()) - started if reached.exists() else None
            when = "never" if late is None else f"{late:.2f}s after the loader started"
            told = f"{err!r} (attempt {attempt}; the tile reached main {when})"
            try:
                # First: a killed tile leaves its nested commands by design,
                # since each runs in a session of its own, so nothing below
                # could pass. A tile that started late is tried again (sd:2333).
                self.assertFalse(killed, "the loader's kill arrived before the tile's refusal; " + told)
                self.assert_none_survive(told)
                self.assertEqual((process.returncode, out), (1, b""))
                self.assertIn(b"launchctl ran past this tile's budget of 4 seconds", err)
                # Not before its deadline either: the cap stopped the command, nothing else did.
                self.assertGreaterEqual(elapsed, sd_tile.TILE_SECONDS - sd_tile.TILE_MARGIN)
                break
            except self.failureException as failure:
                if not tried_again(attempt, late):
                    raise
                say_retry(sys.stderr, self.id(), "toolbox", attempt, failure, when, err)

    def test_a_resource_view_gets_the_tiles_reason_within_its_budget(self):
        # This dashboard's path: the real `sd_tile.py`, read by
        # `reports_screen.collect` within the view's five seconds.
        started = time.monotonic()
        with patch.dict(os.environ, self.environment()), self.assertRaises(ValueError) as refused:
            reports_screen.collect("toolbox")
        elapsed = time.monotonic() - started
        self.assert_none_survive(refused.exception)
        self.assertRegex(str(refused.exception), "^toolbox: PastDeadline.*launchctl ran past this tile's budget")
        self.assertLess(elapsed, reports_screen.VIEW_SECONDS["toolbox"])

    def probe_tile(self):
        script = self.root / "tile.py"
        script.write_text(PROBE_TILE)
        return patch.object(reports_screen, "TILE", script)

    def test_a_hung_vault_probe_still_names_full_disk_access(self):
        # A TCC hang waits forever; the probe declares 15 seconds, past the
        # view's five. Each view that probes the vault gets the actionable
        # reason, from `vault_blocked`, inside its budget.
        reached = self.root / "main"
        for area in ("vault", "briefs"):
            with self.subTest(area=area):
                for attempt in (1, 2, 3):
                    self.reap()
                    self.pids.unlink(missing_ok=True)
                    reached.unlink(missing_ok=True)
                    started = time.monotonic()
                    with self.probe_tile(), patch.dict(os.environ, {**self.environment(), "DEADLINE_MAIN": str(reached)}), \
                            self.assertRaises(ValueError) as refused:
                        reports_screen.collect(area)
                    elapsed = time.monotonic() - started
                    late = float(reached.read_text()) - started if reached.exists() else None
                    when = "never" if late is None else f"{late:.2f}s after the view started"
                    told = f"{refused.exception} (attempt {attempt}; the tile reached main {when})"
                    try:
                        self.assert_none_survive(told)
                        self.assertRegex(str(refused.exception), f"^{area}: RuntimeError.*Grant Full Disk Access", told)
                        self.assertLess(elapsed, reports_screen.VIEW_SECONDS[area], told)
                        break
                    except self.failureException as failure:
                        # `tried_again` and `say_retry` above say why and what;
                        # `RetryNotice` holds both without a real tile.
                        if not tried_again(attempt, late):
                            raise
                        say_retry(sys.stderr, self.id(), area, attempt, failure, when, refused.exception)


def tried_again(attempt, late):
    """Whether a failed attempt of a real tile's check is run once more.

    The deadline counts from `main`, not from process start (sd:760 review
    N4): a tile that reaches `main` about `TILE_MARGIN` late loses to the
    view's kill by design. Only a start past half that margin is tried again
    (sd:763); a prompt one takes hundredths of a second. A cap that does not
    hold fails on a prompt start, and on the last attempt anyway. A tile that
    never reached `main` (`late` is `None`) is tried again too: nothing was
    measured.
    """
    if attempt >= 3:
        return False
    return late is None or late >= sd_tile.TILE_MARGIN / 2


def say_retry(stream, test_id, area, attempt, failure, when, told):
    """Write the retry notice to `stream`, so a log shows a retry absorbed a failure.

    Said out loud (sd:769), and naming which check it was: the view's text
    fits more than one (sd:783). On a line of its own, not after the runner's
    progress dots. The first line of the failure is the check; the rest is
    unittest's diff, which the notice does not repeat.
    """
    check = str(failure).partition("\n")[0]
    print(f"\n{test_id} [{area}]: attempt {attempt} failed and is tried again; "
          f"the check that failed: {check}; "
          f"the tile reached main {when}; the view was told: {told}",
          file=stream)


class RetryNotice(unittest.TestCase):
    """The retry rule and its notice, driven without a real four-second tile.

    review-346: the notice above was reached only when real timing put a
    tile's start past half the margin, and nothing captured it, so a notice
    that stopped printing or dropped the check's name would have failed no
    test. Both halves are functions now, and this drives each with the
    inputs the loop would give them.
    """

    def test_a_late_start_is_tried_again_and_a_prompt_one_is_not(self):
        half = sd_tile.TILE_MARGIN / 2
        self.assertTrue(tried_again(1, half))
        self.assertTrue(tried_again(2, half + 0.1))
        self.assertTrue(tried_again(1, None))
        self.assertFalse(tried_again(1, half - 0.01))
        self.assertFalse(tried_again(1, 0.02))
        # The last attempt raises whatever the timing was.
        self.assertFalse(tried_again(3, half + 0.5))
        self.assertFalse(tried_again(3, None))

    def test_the_notice_names_the_check_the_attempt_and_the_tiles_start(self):
        stream = io.StringIO()
        failure = AssertionError("Regex didn't match: 'vault: RuntimeError' not found\n- a\n+ b")
        say_retry(stream, "tests.test_tile_deadline.X.test_y", "vault", 2, failure,
                  "0.61s after the view started", "vault: PastDeadline: budget spent")
        lines = stream.getvalue().split("\n")
        # A blank line first: the notice must not land after the runner's dots.
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[1],
                         "tests.test_tile_deadline.X.test_y [vault]: attempt 2 failed and is tried again; "
                         "the check that failed: Regex didn't match: 'vault: RuntimeError' not found; "
                         "the tile reached main 0.61s after the view started; "
                         "the view was told: vault: PastDeadline: budget spent")
        # The diff below the failure's first line is not repeated.
        self.assertNotIn("+ b", stream.getvalue())


class Calls(unittest.TestCase):
    """What `run` hands `communicate`, with and without a deadline."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("deadline_collectors", HERE / "collectors.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.started = []
        test = self

        class Child:
            pid = 0

            def __init__(self, argv, **_):
                test.started.append(argv)

            def communicate(self, timeout=None):
                test.timeouts.append(timeout)
                return "done\n", None

        self.timeouts = []
        popen = patch.object(self.module.subprocess, "Popen", Child)
        popen.start()
        self.addCleanup(popen.stop)

    def test_without_a_deadline_run_keeps_its_timeout(self):
        self.assertEqual(self.module.run(["launchctl", "list"]), "done")
        self.module.run(["docker", "ps"], timeout=10)
        self.module.run(["probe"], timeout=15)
        # Each wait is the command's own timeout less what creating the child
        # cost, never more: the stop is fixed before `Popen` and measured
        # after it. `assertEqual` here would have to trust that `Popen` takes
        # no time, which on a loaded host it does not.
        for waited, asked in zip(self.timeouts, (25, 10, 15)):
            self.assertTrue(asked - 1.0 < waited <= asked, (waited, asked))
        self.assertTrue(4.0 < self.module.Budget(5.0).stop - time.monotonic() <= 5.0)

    def test_a_deadline_caps_only_what_runs_past_it(self):
        with self.module.set_deadline(12.0, started=time.monotonic()):
            self.module.run(["docker", "ps"], timeout=10)
            self.module.run(["launchctl", "list"])
        self.module.run(["launchctl", "list"])
        self.assertTrue(9.0 < self.timeouts[0] <= 10, self.timeouts)
        self.assertTrue(11.0 < self.timeouts[1] <= 12.0, self.timeouts)
        # And only while the tile builds its tab.
        self.assertTrue(24.0 < self.timeouts[2] <= 25, self.timeouts)

    def test_the_wait_is_measured_after_the_child_is_created(self):
        # The deadline is absolute, so process creation comes out of it. The
        # timeout used to be handed to `communicate` unchanged, which spent
        # that time twice and put the wait past the deadline by however long
        # `Popen` took. A fifth of a second is stood in for a loaded host's
        # fork; the point is the direction, not the number. Only the upper
        # bound is asserted, because a real fork on a slow runner comes out
        # of the same second and lowers the wait further: CI measured 0.69
        # against a 0.7 floor and reddened a correct change.
        test = self

        class Slow:
            pid = 0

            def __init__(self, argv, **_):
                time.sleep(0.2)

            def communicate(self, timeout=None):
                test.timeouts.append(timeout)
                return "done\n", None

        with patch.object(self.module.subprocess, "Popen", Slow):
            self.module.run(["docker", "ps"], timeout=1)
        self.assertTrue(0.0 < self.timeouts[-1] <= 0.8, self.timeouts)

    def test_a_capped_wait_stops_at_the_deadline_and_not_at_now_plus_left(self):
        # The deadline is absolute, so the stop for a capped wait is `DEADLINE`
        # itself. It was rebuilt as `monotonic() + left` instead, and `left`
        # had been sampled further up, so everything spent between the two
        # lines was handed back to the tile and the wait ran past the deadline
        # by that much (Copilot review of this branch). Half a second stands
        # in for that gap, as a fifth of a second stands in for a fork above;
        # the point is the direction, not the number. Only the upper bound is
        # asserted, because a loaded host lowers the wait further.
        real = self.module.time_left

        def sampled_early():
            left = real()
            time.sleep(0.5)
            return left

        with self.module.set_deadline(2.0, started=time.monotonic()):
            with patch.object(self.module, "time_left", sampled_early):
                self.module.run(["docker", "ps"], timeout=10)
        self.assertTrue(0.0 < self.timeouts[-1] <= 1.7, self.timeouts)

    def test_an_unbounded_timeout_under_a_deadline_is_the_deadline(self):
        # `timeout=None` is a command with no ceiling of its own, which
        # `Budget.run` already accepts. Compared with the seconds left it
        # raised TypeError, so inside a tile it could not be expressed at all.
        with self.module.set_deadline(12.0, started=time.monotonic()):
            self.module.run(["launchctl", "list"], timeout=None)
        self.assertTrue(11.0 < self.timeouts[-1] <= 12.0, self.timeouts)

    def test_a_spent_deadline_refuses_a_zero_timeout(self):
        # `left <= 0` and `timeout == 0`: the strict `left < timeout` was
        # false, so the command started although the tile had no time to wait
        # with it. The contract says a spent deadline starts nothing.
        with self.module.set_deadline(4.0, started=time.monotonic() - 4.0):
            with self.assertRaisesRegex(self.module.OverBudget, "launchctl was not started"):
                self.module.run(["launchctl", "list"], timeout=0)
        self.assertEqual(self.started, [])

    def test_no_time_left_refuses_without_starting(self):
        with self.module.set_deadline(4.0, started=time.monotonic() - 4.0):
            with self.assertRaisesRegex(self.module.OverBudget, "launchctl was not started") as refused:
                self.module.run(["launchctl", "list"])
            self.assertFalse(refused.exception.started)
            with self.assertRaisesRegex(self.module.OverBudget, "machine-setup.sh was not started"):
                self.module.Budget(5.0).run(["machine-setup.sh", "candidates", "service"])
            # A probe that never ran has no silence to read as refused.
            with self.assertRaisesRegex(self.module.OverBudget, "not started"):
                self.module.vault_blocked()
        self.assertEqual(self.started, [])


def fresh(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A command whose grandchild leaves its session, so the kill of the command's
# group misses it, and keeps the stdout pipe it inherited open for a minute.
ESCAPING = ("import os, subprocess, sys, time\n"
            "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "                              start_new_session=True)\n"
            "open(os.environ['DEADLINE_PIDS'], 'a').write(f'{os.getpid()}\\n{grandchild.pid}\\n')\n"
            "time.sleep(60)\n")


class Deadlines(unittest.TestCase):
    """The deadline itself: how long `run` waits, and how `with`s nest (sd:763)."""

    def setUp(self):
        self.module = fresh("deadline_collectors", HERE / "collectors.py")

    def test_the_wait_after_a_command_timeout_stops_at_the_deadline(self):
        # The command times out on its own three seconds, inside a four-second
        # deadline, so `run` kills its group and waits for the pipe -- which the
        # escaped grandchild holds. Uncapped that wait is five seconds more, so
        # never less than eight in all; capped it ends at the deadline.
        temporary = tempfile.TemporaryDirectory(prefix="tile-deadline-")
        self.addCleanup(temporary.cleanup)
        pids = Path(temporary.name) / "pids"

        def reap():
            for pid in (int(line) for line in pids.read_text().split()) if pids.exists() else ():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.addCleanup(reap)
        started = time.monotonic()
        with patch.dict(os.environ, {"DEADLINE_PIDS": str(pids)}), \
                self.module.set_deadline(4.0, started=started):
            out = self.module.run([sys.executable, "-c", ESCAPING], timeout=3)
        elapsed = time.monotonic() - started
        # The shape really happened: the grandchild started, and outlived the kill.
        recorded = pids.read_text().split() if pids.exists() else []
        self.assertEqual(len(recorded), 2, "the command and its grandchild did not both start")
        grandchild = int(recorded[1])
        self.assertTrue(running(grandchild), "the grandchild did not hold the pipe past the kill")
        self.assertEqual(out, "")
        # Not a tight bound: under load the only claim is that the wait stopped
        # a second or more short of its uncapped five past the command's three.
        self.assertLess(elapsed, 3 + 5 - 1, "the wait after the kill ignored the tile's deadline")

    def test_the_wait_after_a_timeout_is_five_seconds_unless_the_deadline_is_nearer(self):
        timeouts = []

        # The module's clock only: a command that times out has spent its
        # timeout, so the wait after the kill must be counted from then, not
        # from before the command started (sd:769).
        class Clock:
            now = 1000.0

            @classmethod
            def monotonic(cls):
                return cls.now

        class Child:
            pid = 0

            def __init__(self, argv, **_):
                pass

            def communicate(self, timeout=None):
                timeouts.append(timeout)
                if len(timeouts) == 1:
                    Clock.now += timeout
                    raise subprocess.TimeoutExpired("probe", timeout)
                return "late\n", None

        with patch.object(self.module.subprocess, "Popen", Child), \
                patch.object(self.module.os, "killpg"), patch.object(self.module, "time", Clock):
            self.assertEqual(self.module.run(["probe"], timeout=1), "late")
            self.assertEqual(timeouts, [1, 5])
            with self.module.set_deadline(30.0, started=Clock.now):
                timeouts.clear()
                self.module.run(["probe"], timeout=1)
                self.assertEqual(timeouts, [1, 5])
            with self.module.set_deadline(3.0, started=Clock.now):
                timeouts.clear()
                self.module.run(["probe"], timeout=1)
                # Three seconds less the one the command spent, not the three
                # there were before it started. The clock is fake, so exactly
                # that: not a second less either (sd:783).
                self.assertEqual(timeouts, [1, 2.0])

    def test_an_inner_deadline_hands_the_outer_one_back(self):
        now = time.monotonic()
        with self.module.set_deadline(30.0, started=now):
            with self.module.set_deadline(2.0, started=now):
                self.assertLessEqual(self.module.time_left(), 2.0)
                self.assertEqual(self.module.DEADLINE_SECONDS, 2.0)
            self.assertEqual((self.module.DEADLINE, self.module.DEADLINE_SECONDS), (now + 30.0, 30.0))
            with self.assertRaises(KeyError), self.module.set_deadline(1.0, started=now):
                raise KeyError("an inner tab failed")
            self.assertEqual((self.module.DEADLINE, self.module.DEADLINE_SECONDS), (now + 30.0, 30.0))
        self.assertIsNone(self.module.time_left())
        self.assertIsNone(self.module.DEADLINE_SECONDS)


class TileClock(unittest.TestCase):
    """The tile's clock starts in `main`, not when `sd_tile` is imported (sd:763).

    A real tile imports and runs within milliseconds, so no timing test can
    tell the two apart. A process that imports `sd_tile` and calls `main`
    later can: taken at import, its deadline would already be spent.
    """

    def test_the_deadline_is_taken_when_main_runs(self):
        tile = fresh("deadline_sd_tile", HERE / "sd_tile.py")
        deadlines = []
        loading = []

        class Collectors:
            @staticmethod
            @contextlib.contextmanager
            def set_deadline(seconds, *, started):
                deadlines.append((seconds, started))
                yield

        def load_collectors():
            # Loading `collectors.py` is part of the tile's time (sd:769). The
            # sleep makes a clock started after the load miss by a wide margin.
            loading.append(time.monotonic())
            time.sleep(0.05)
            return Collectors

        called = time.monotonic()
        with patch.object(tile, "load_collectors", load_collectors), \
                patch.dict(tile.TABS, {"toolbox": lambda collectors: {"html": ""}}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tile.main(["toolbox"]), 0)
        [(seconds, started)] = deadlines
        self.assertEqual(seconds, tile.TILE_SECONDS - tile.TILE_MARGIN)
        self.assertGreaterEqual(started, called, "the tile's clock started before main was called")
        self.assertLessEqual(started, loading[0], "the tile's clock started after collectors.py was loaded")


if __name__ == "__main__":
    unittest.main()
