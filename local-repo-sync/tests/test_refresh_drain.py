"""Tests for the lane drain in front of `repo-sync.sh refresh` (sd:3099).

Before refresh moves a pinned checkout, `refresh_drain.py` takes every
lane's `runner.lock` under the lane root and waits for an idle gate. While
it holds a lock, that lane's `lane run` exits at once. The lane root here is
a fixture folder with real lock files, `sd` is the stub in `test_pinned.py`,
and the test holds a lock from a separate process, as a runner would.

Each case names its kind, as in `test_repo_sync.py`. The cases here are all
NEW: written before the drain existed and seen to fail against the script
without it.
"""

import pathlib
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_pinned import PinFixture  # noqa: E402
from test_repo_sync import FOLDER  # noqa: E402

HOLDER = """
import fcntl, sys, time
handle = open(sys.argv[1], "a")
fcntl.flock(handle, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
"""

PROBE = """
import fcntl, sys
handle = open(sys.argv[1], "a")
try:
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("free")
except BlockingIOError:
    print("held")
"""

# Answers `sd gate status --json` busy for the first two calls, then idle.
GATE_BUSY_TWICE = """
n=$(cat "$GATE_COUNT" 2>/dev/null || echo 0)
echo $((n + 1)) > "$GATE_COUNT"
if [ "$n" -lt 2 ]; then
  echo '{"holders": [{"pid": 1}], "waiters": []}'
else
  echo '{"holders": [], "waiters": []}'
fi
"""


class DrainFixture(PinFixture):
    def __init__(self):
        super().__init__()
        self.probe = self.tmp / "probe.py"
        self.probe.write_text(PROBE)

    def lane(self, name):
        lock = self.lanes / name / "lane" / "queue" / "runner.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        return lock

    def hold(self, lock, seconds):
        """Hold `lock` from another process for `seconds`, as a runner does."""
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock), str(seconds)],
                                stdout=subprocess.PIPE, text=True)
        self.assert_line(proc.stdout.readline(), "locked")
        return proc

    @staticmethod
    def assert_line(line, want):
        if line.strip() != want:
            raise AssertionError(f"wanted {want!r}, got {line!r}")

    def state(self, lock):
        out = subprocess.run([sys.executable, str(self.probe), str(lock)],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def pinned_behind(self, rel="a/proj"):
        repo = self.repo(rel)
        self.pin(repo)
        before = self.head(repo)
        new = self.advance_origin(repo)
        return repo, before, new

    def pinned_pack(self):
        pack = self.repo("a/pack")
        (pack / "bin").mkdir()
        self.commit(pack, "bin/sd_install.py", "", "installer")
        self.git(pack, "push", "-q", "origin", "main")
        self.pin(pack)
        new = self.advance_origin(pack)
        return pack, new


class DrainTest(unittest.TestCase):
    def fixture(self):
        f = DrainFixture()
        self.addCleanup(f.destroy)
        return f

    def holder(self, f, lock, seconds):
        proc = f.hold(lock, seconds)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))
        return proc

    def test_the_bound_is_45_minutes_for_locks_and_gate_together(self):
        """NEW. Operator ruling: one 45-minute bound covers the lock wait and
        the gate wait together, and help says so."""
        f = self.fixture()
        sys.path.insert(0, str(FOLDER))
        try:
            import refresh_drain
        finally:
            sys.path.remove(str(FOLDER))
        self.assertEqual(45 * 60, refresh_drain.DEFAULT_WAIT)
        help_text = " ".join(f.run("help").stdout.split())
        self.assertIn("45 minutes in total for the lane locks and the gate together", help_text)

    def test_a_held_lock_makes_refresh_wait_then_proceed(self):
        """NEW. A busy lane holds its runner lock; refresh says it waits on
        that lane, and moves the checkout once the runner lets go."""
        f = self.fixture()
        repo, _, new = f.pinned_behind()
        lock = f.lane("busy-repo")
        self.holder(f, lock, 2)

        started = time.monotonic()
        result = f.run("refresh", expect=0)

        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertIn("waiting for lane busy-repo", result.stdout)
        self.assertEqual(new, f.head(repo))

    def test_a_lock_held_past_the_bound_refuses_and_moves_nothing(self):
        """NEW. The wait is bounded. Past it, refresh names the busy lane,
        moves nothing, exits 1, and leaves no lock taken."""
        f = self.fixture()
        repo, before, _ = f.pinned_behind()
        free = f.lane("a-idle-repo")
        busy = f.lane("busy-repo")
        proc = self.holder(f, busy, 30)

        result = f.run("refresh", expect=1, extra_env={"REPO_SYNC_DRAIN_WAIT": "2"})

        self.assertIn("busy-repo", result.stdout + result.stderr)
        self.assertIn("nothing moved", result.stdout + result.stderr)
        self.assertEqual(before, f.head(repo))
        self.assertEqual("free", f.state(free))
        proc.kill()
        proc.wait()
        self.assertEqual("free", f.state(busy))

    def test_a_lane_run_during_the_refresh_finds_the_lock_held(self):
        """NEW. While the refresh steps run, each lane's runner lock is held,
        so `lane run` (which tries it once) exits at once."""
        f = self.fixture()
        pack, new = f.pinned_pack()
        lock = f.lane("some-repo")
        hook = f'{sys.executable} {f.probe} {lock} >> {f.tmp / "during"}'

        f.run("refresh", expect=0, extra_env={"MAKE_HOOK": hook})

        self.assertEqual("held\n", (f.tmp / "during").read_text())
        self.assertEqual(new, f.head(pack))
        self.assertEqual("free", f.state(lock))

    def test_the_child_does_not_inherit_the_locks(self):
        """NEW. Only the helper holds the locks: kill it mid-refresh and the
        kernel frees them, even while the refresh child still runs."""
        f = self.fixture()
        f.pinned_pack()
        lock = f.lane("some-repo")
        during = f.tmp / "during"
        # make's parent is the refresh shell; that shell's parent is the helper.
        # Anything else is not the helper, and is left alone.
        hook = (f'helper=$(ps -o ppid= -p $PPID | tr -d " "); '
                f'case "$(ps -o command= -p "$helper")" in */local-repo-sync/refresh_drain.py*) ;; '
                f'*) echo nohelper >> {during}; exit 0 ;; esac; '
                f'kill -9 "$helper"; sleep 0.2; '
                f'{sys.executable} {f.probe} {lock} >> {during}')

        f.run("refresh", expect=None, extra_env={"MAKE_HOOK": hook})

        self.assertEqual("free\n", during.read_text())

    def test_the_gate_wait_waits_for_an_idle_gate(self):
        """NEW. With every lock held, refresh waits until `sd gate status
        --json` shows no holders and no waiters."""
        f = self.fixture()
        repo, _, new = f.pinned_behind()
        script = f.tmp / "gate.sh"
        script.write_text(GATE_BUSY_TWICE)

        result = f.run("refresh", expect=0, extra_env={
            "SD_GATE_SCRIPT": str(script), "GATE_COUNT": str(f.tmp / "gate.count")})

        self.assertIn("waiting for the gate: 1 holder(s), 0 waiter(s)", result.stdout)
        self.assertEqual("3", (f.tmp / "gate.count").read_text().strip())
        self.assertEqual(new, f.head(repo))

    def test_a_busy_gate_past_the_bound_refuses(self):
        """NEW. A gate that stays busy refuses like a busy lane."""
        f = self.fixture()
        repo, before, _ = f.pinned_behind()
        script = f.tmp / "gate.sh"
        script.write_text("echo '{\"holders\": [], \"waiters\": [{\"pid\": 1}]}'\n")

        result = f.run("refresh", expect=1, extra_env={
            "SD_GATE_SCRIPT": str(script), "REPO_SYNC_DRAIN_WAIT": "2"})

        self.assertIn("gate", result.stdout + result.stderr)
        self.assertIn("nothing moved", result.stdout + result.stderr)
        self.assertEqual(before, f.head(repo))

    def test_a_failing_gate_status_refuses(self):
        """NEW. An unreadable gate is not an idle one. The refusal names the
        manual move, for when a broken pack is what breaks `sd`."""
        f = self.fixture()
        repo, before, _ = f.pinned_behind()
        script = f.tmp / "gate.sh"
        script.write_text("echo boom >&2; exit 3\n")

        result = f.run("refresh", expect=1, extra_env={"SD_GATE_SCRIPT": str(script)})

        self.assertIn("git -C <checkout> switch --detach origin/main", result.stderr)
        self.assertEqual(before, f.head(repo))

    def test_the_lane_root_setting_is_read_when_no_variable_names_one(self):
        """NEW. As the pack's lane_root(): SD_LANE_ROOT, else the sd.lane_root
        setting, else the state default."""
        f = self.fixture()
        f.pinned_behind()
        other = f.tmp / "configured-lanes"
        lock = other / "set-repo" / "lane" / "queue" / "runner.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        self.holder(f, lock, 30)

        result = f.run("refresh", expect=1, extra_env={
            "SD_LANE_ROOT": None, "SD_LANE_ROOT_SETTING": str(other),
            "REPO_SYNC_DRAIN_WAIT": "1"})

        self.assertIn("set-repo", result.stdout + result.stderr)

    def test_the_state_default_is_the_last_resort(self):
        """NEW. With no variable and no setting, the root is
        $XDG_STATE_HOME/sd/lanes."""
        f = self.fixture()
        f.pinned_behind()
        state = f.tmp / "state"
        lock = state / "sd" / "lanes" / "default-repo" / "lane" / "queue" / "runner.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        self.holder(f, lock, 30)

        result = f.run("refresh", expect=1, extra_env={
            "SD_LANE_ROOT": None, "XDG_STATE_HOME": str(state),
            "REPO_SYNC_DRAIN_WAIT": "1"})

        self.assertIn("default-repo", result.stdout + result.stderr)

    def test_a_broken_lane_root_setting_refuses(self):
        """NEW. A setting sd cannot read is not "unset": falling back to the
        default would drain the wrong folder."""
        f = self.fixture()
        repo, before, _ = f.pinned_behind()

        result = f.run("refresh", expect=1, extra_env={
            "SD_LANE_ROOT": None, "SD_CONFIG_BROKEN": "1"})

        self.assertIn("sd.lane_root", result.stderr)
        self.assertEqual(before, f.head(repo))

    def test_an_unopenable_lock_refuses(self):
        """NEW. A lock refresh cannot open is a lane it cannot hold."""
        f = self.fixture()
        repo, before, _ = f.pinned_behind()
        lock = f.lane("locked-out")
        lock.chmod(0)
        self.addCleanup(lock.chmod, 0o644)

        result = f.run("refresh", expect=1)

        self.assertIn(str(lock), result.stderr)
        self.assertEqual(before, f.head(repo))

    def test_a_lane_that_appears_during_the_drain_is_held_too(self):
        """NEW. A lane folder made while refresh waits on the gate is held
        before any checkout moves."""
        f = self.fixture()
        pack, new = f.pinned_pack()
        late = f.lanes / "late-repo" / "lane" / "queue" / "runner.lock"
        script = f.tmp / "gate.sh"
        script.write_text(f'mkdir -p {late.parent}; touch {late}\n'
                          'echo \'{"holders": [], "waiters": []}\'\n')
        hook = f'{sys.executable} {f.probe} {late} >> {f.tmp / "during"}'

        f.run("refresh", expect=0, extra_env={
            "SD_GATE_SCRIPT": str(script), "MAKE_HOOK": hook})

        self.assertEqual("held\n", (f.tmp / "during").read_text())
        self.assertEqual(new, f.head(pack))

    def test_locks_release_after_a_failed_refresh(self):
        """NEW. A refresh step that fails exits 1 with its own message, and
        every lock is free afterwards."""
        f = self.fixture()
        repo, before, _ = f.pinned_behind()
        lock = f.lane("some-repo")
        (repo / "README").write_text("local edit\n")

        result = f.run("refresh", expect=1)

        self.assertIn("uncommitted changes", result.stdout)
        self.assertIn("lanes held", result.stdout)
        self.assertEqual("free", f.state(lock))
        self.assertEqual(before, f.head(repo))


if __name__ == "__main__":
    unittest.main()
