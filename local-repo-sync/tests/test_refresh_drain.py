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

    def test_kill_9_of_the_helper_leaves_the_locks_held_until_the_child_ends(self):
        """NEW (review round 4). The refresh child holds the locks too: kill -9
        of the helper mid-refresh leaves them held until the child exits."""
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

        self.assertEqual("held\n", during.read_text())
        self.assertEqual("free", f.state(lock))

    def test_git_in_the_refresh_leaves_no_process_behind(self):
        """NEW (review round 4). A detached gc or maintenance run, or an
        fsmonitor daemon, would hold the inherited locks after the refresh;
        the child's git runs each in the foreground or not at all."""
        f = self.fixture()
        f.pinned_pack()
        during = f.tmp / "during"
        hook = "; ".join(f"git config --get {key} >> {during}"
                         for key in ("gc.autoDetach", "maintenance.autoDetach", "core.fsmonitor"))

        f.run("refresh", expect=0, extra_env={"MAKE_HOOK": hook})

        self.assertEqual("false\nfalse\nfalse\n", during.read_text())

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
        # Two busy, one idle, and one more after the last lock pass.
        self.assertEqual("4", (f.tmp / "gate.count").read_text().strip())
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


class ExclusionTest(unittest.TestCase):
    """Review round 3: the drain excludes every lane run and gate for the
    whole time the checkouts move. One case per row of the PR's table."""

    def fixture(self):
        f = DrainFixture()
        self.addCleanup(f.destroy)
        return f

    def test_a_signal_to_the_helper_keeps_the_locks_until_the_child_ends(self):
        """NEW. TERM or INT to the helper alone reaches the refresh child,
        and the helper holds every lock until that child has exited."""
        for name in ("TERM", "INT", "HUP"):
            with self.subTest(signal=name):
                f = self.fixture()
                f.pinned_pack()
                lock = f.lane("some-repo")
                during = f.tmp / "during"
                hook = (f'helper=$(ps -o ppid= -p $PPID | tr -d " "); '
                        f'case "$(ps -o command= -p "$helper")" in */local-repo-sync/refresh_drain.py*) ;; '
                        f'*) echo nohelper >> {during}; exit 0 ;; esac; '
                        f'kill -{name} "$helper"; sleep 0.5; '
                        f'{sys.executable} {f.probe} {lock} >> {during}')

                f.run("refresh", expect=None, extra_env={"MAKE_HOOK": hook})

                self.assertEqual("held\n", during.read_text())
                self.assertEqual("free", f.state(lock))

    def test_a_lane_folder_without_a_lock_file_is_held(self):
        """NEW. A lane folder whose runner never ran has no runner.lock yet;
        the drain makes it and holds it, so a first `lane run` exits."""
        f = self.fixture()
        f.pinned_pack()
        (f.lanes / "bare-repo" / "lane").mkdir(parents=True)
        lock = f.lanes / "bare-repo" / "lane" / "queue" / "runner.lock"
        hook = f'mkdir -p {lock.parent}; {sys.executable} {f.probe} {lock} >> {f.tmp / "during"}'

        f.run("refresh", expect=0, extra_env={"MAKE_HOOK": hook})

        self.assertEqual("held\n", (f.tmp / "during").read_text())

    def test_a_conf_repo_with_no_lane_folder_is_held(self):
        """NEW. A repository in the conf whose lane never ran has no folder;
        a lane folder is named after the checkout, so the drain makes and
        holds that lock too."""
        f = self.fixture()
        f.pinned_pack()
        lock = f.lanes / "pack" / "lane" / "queue" / "runner.lock"
        hook = f'mkdir -p {lock.parent}; {sys.executable} {f.probe} {lock} >> {f.tmp / "during"}'

        f.run("refresh", expect=0, extra_env={"MAKE_HOOK": hook})

        self.assertEqual("held\n", (f.tmp / "during").read_text())

    def test_a_registered_repo_with_no_lane_folder_is_held(self):
        """NEW (review round 4). A repository in the registry (`sd-db.sh repo
        list`, which `lane run --hosted` reads) and not in the conf, whose
        lane never ran: the drain makes and holds its lock, named after the
        checkout's folder."""
        f = self.fixture()
        f.pinned_pack()
        listing = f.tmp / "repo-list"
        listing.write_text(f"sd-db: {f.tmp / 'elsewhere' / 'registered repo'}  -  file  yes  local  off  hub  auto\n")
        lock = f.lanes / "registered repo" / "lane" / "queue" / "runner.lock"
        hook = f'mkdir -p "{lock.parent}"; {sys.executable} {f.probe} "{lock}" >> {f.tmp / "during"}'

        f.run("refresh", expect=0, extra_env={"MAKE_HOOK": hook, "REPO_LIST": str(listing)})

        self.assertEqual("held\n", (f.tmp / "during").read_text())

    def test_an_unreadable_repo_registry_refuses(self):
        """NEW (review round 4). A registry the drain cannot read is a lane
        list it cannot hold: refuse, nothing moved, with the manual move."""
        f = self.fixture()
        repo, before, _ = f.pinned_behind()

        result = f.run("refresh", expect=1, extra_env={"REPO_LIST_RC": "3"})

        self.assertIn("repo list", result.stderr)
        self.assertIn("git -C <checkout> switch --detach origin/main", result.stderr)
        self.assertEqual(before, f.head(repo))

    def test_the_gate_is_checked_again_after_the_last_lock_pass(self):
        """NEW. A gate that starts after the first idle check and before the
        refresh is seen: the last step before the child is a gate check."""
        f = self.fixture()
        repo, _, new = f.pinned_behind()
        f.lane("some-repo")
        script = f.tmp / "gate.sh"
        script.write_text('''
n=$(cat "$GATE_COUNT" 2>/dev/null || echo 0)
echo $((n + 1)) > "$GATE_COUNT"
if [ "$n" -eq 1 ]; then
  echo '{"holders": [{"pid": 1}], "waiters": []}'
else
  echo '{"holders": [], "waiters": []}'
fi
''')

        result = f.run("refresh", expect=0, extra_env={
            "SD_GATE_SCRIPT": str(script), "GATE_COUNT": str(f.tmp / "gate.count")})

        self.assertIn("waiting for the gate: 1 holder(s)", result.stdout)
        self.assertEqual(new, f.head(repo))


if __name__ == "__main__":
    unittest.main()
