"""Tests for tile actions on their host: follow answers the hub's tile requests (pack sd:6387).

A hub tile line that names this machine writes a request row,
`place_tiles.request.<host>.<realm>.<tile>.<action>`, in the hub's database.
This satellite's `repo-sync.sh follow` runs the act line of its own
place-tiles.conf, never a command from the row, through the pinned pack's
`place_tiles.run`, and writes the result row in steps: starting, then running
or refused, then done once the run ends. The database is a real one at the
pinned pack's schema, the checkouts are test_satellite_refresh.py's.

Each case names its kind: NEW was written before follow answered a tile
request, and seen to fail against the script without it.
"""

import fcntl
import json
import pathlib
import shutil
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_refresh_migrate import source_pack  # noqa: E402
from test_satellite_refresh import HOST, RefreshFixture  # noqa: E402

FOLDER = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FOLDER))
import satellite_tiles  # noqa: E402


class TilesFixture(RefreshFixture):
    """A satellite with the real satellite_tiles.py, the pinned pack's dashboard, and one tile in its own file."""

    def __init__(self):
        super().__init__()
        shutil.copy(FOLDER / "satellite_tiles.py", self.folder / "satellite_tiles.py")
        shutil.copytree(source_pack() / "dashboard" / "sd_dashboard", self.sd_pack / "dashboard" / "sd_dashboard",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        self.app = self.tmp / "app"
        self.app.mkdir()
        self.config = self.tmp / "config"
        (self.config / "project-dashboard").mkdir(parents=True)
        self.conf(f"tile|work|sim|Simulator|{self.app}|-\n"
                  "act|sim|Start|sh -c \"echo ran >> runs; echo started\"\n"
                  "act|sim|Stop|true\n"
                  f"tile|work|gone|Gone|{self.tmp / 'not-here'}|-\nact|gone|Start|true\n")
        self.states = self.tmp / "state" / "system" / "place-tiles" / "work"

    def conf(self, text):
        (self.config / "project-dashboard" / "place-tiles.conf").write_text(text)

    def follow(self, expect=0):
        return self.run("follow", expect=expect, extra_env={"SYSTEM_TOOLS_CONFIG": str(self.config)})

    def key(self, kind, tile="sim", action="start"):
        return satellite_tiles.key(kind, HOST, "work", tile, action)

    def ask(self, tile="sim", action="Start", rid="ab12", **more):
        """The hub's write for a button: the row the pack's place_tiles writes."""
        self.put(self.key(satellite_tiles.REQUEST, tile, action.lower()),
                 {"id": rid, "at": "2026-10-26T12:00:00+00:00", "host": HOST, "realm": "work", "tile": tile,
                  "action": action, **more})
        return rid

    def answer(self, tile="sim", action="start"):
        return self.setting(self.key(satellite_tiles.RESULT, tile, action))

    def runs(self):
        try:
            return (self.app / "runs").read_text().count("ran")
        except FileNotFoundError:
            return 0

    def ended(self, action="start"):
        exit_file = self.states / "sim" / action / "exit"
        for _ in range(200):
            if exit_file.exists():
                return
            time.sleep(0.05)
        raise AssertionError(f"{exit_file} never came")


class TilesTest(unittest.TestCase):
    def fixture(self):
        f = TilesFixture()
        self.addCleanup(f.destroy)
        return f

    def test_the_key_is_the_one_the_pack_writes(self):
        """NEW. The pack's place_tiles.key writes this literal (its own test
        pins it too): one rule, spelled in both repositories."""
        self.assertEqual(satellite_tiles.key(satellite_tiles.REQUEST, "far-host", "work", "sim", "start"),
                         "place_tiles.request.far_host.work.sim.start")
        self.assertEqual(satellite_tiles.key(satellite_tiles.RESULT, "1mac", "work", "9sim", "tear-down"),
                         "place_tiles.result.x1mac.work.x9sim.tear_down")

    def test_a_request_runs_this_machines_act_line_and_the_result_follows_it_to_done(self):
        """NEW. A request runs the act line of this machine's own file in its
        folder, never the row's command, and writes running; the next run,
        once the command ended, writes done with its exit and output."""
        f = self.fixture()
        f.ask(command="touch pwned")

        out = f.follow()

        self.assertIn("tiles: sim Start: running", out.stdout)
        row = f.answer()
        self.assertEqual((row["request"], row["state"], row["attempt"], row["host"], row["tile"], row["action"]),
                         ("ab12", "running", 1, HOST, "sim", "Start"))
        f.ended()
        self.assertEqual(f.runs(), 1)
        self.assertFalse((f.app / "pwned").exists(), "the row's command ran")

        f.follow()
        row = f.answer()
        self.assertEqual((row["state"], row["exit"], row["output"]), ("done", 0, "started\n"))
        self.assertIsNone(f.answer(action="stop"), "an action nobody asked for was answered")

        f.follow()
        self.assertEqual(f.runs(), 1, "an answered request ran again")

    def test_a_start_that_cannot_run_here_is_refused_and_runs_nothing(self):
        """NEW. A folder not on this machine, or a run that holds the action's
        lock, is `refused` with the reason: nothing ran."""
        f = self.fixture()
        f.ask(tile="gone")
        f.follow()
        row = f.answer(tile="gone")
        self.assertEqual(row["state"], "refused")
        self.assertIn("is not on this machine", row["reason"])

        state = f.states / "sim" / "start"
        state.mkdir(parents=True)
        with open(state / "lock", "a+", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            f.ask()
            f.follow()
        row = f.answer()
        self.assertEqual(row["state"], "refused")
        self.assertIn("already running", row["reason"])
        self.assertEqual(f.runs(), 0)

    def test_a_row_left_starting_after_the_start_is_adopted_not_run_twice(self):
        """NEW. Stopped between the start and its `running` write: the start
        here since `starting` is the request's, so it is not run again."""
        f = self.fixture()
        f.ask()
        f.follow()
        f.ended()
        f.put(f.key(satellite_tiles.RESULT), {**f.answer(), "state": "starting", "started": "2026-10-26T00:00:00+00:00"})

        f.follow()

        self.assertEqual(f.runs(), 1, "the adopted start ran again")
        self.assertEqual(f.answer()["state"], "done")

    def test_a_row_left_starting_before_the_start_runs_once_more_then_is_refused(self):
        """NEW. Stopped before the start: attempt 2 runs it; a second stop is
        refused, and nothing runs again until a new request."""
        f = self.fixture()
        f.ask()
        later = "2999-01-01T00:00:00+00:00"
        f.put(f.key(satellite_tiles.RESULT), {"host": HOST, "realm": "work", "tile": "sim", "action": "Start",
                                              "request": "ab12", "attempt": 1, "state": "starting", "started": later})
        f.follow()
        self.assertEqual((f.answer()["attempt"], f.answer()["state"]), (2, "running"))
        f.ended()
        self.assertEqual(f.runs(), 1)

        f.put(f.key(satellite_tiles.RESULT), {**f.answer(), "state": "starting", "started": later})
        f.follow()
        row = f.answer()
        self.assertEqual((row["attempt"], row["state"]), (2, "refused"))
        self.assertIn("attempt 2 stopped", row["reason"])
        self.assertEqual(f.runs(), 1)

    def test_a_row_for_another_action_is_named_and_follow_goes_on(self):
        """NEW. A request row that names another action is not answered: the
        run says so with a Reports line, and follow still follows."""
        f = self.fixture()
        f.put(f.key(satellite_tiles.REQUEST), {"id": "ab12", "at": "2026-10-26T12:00:00+00:00", "host": HOST,
                                               "realm": "work", "tile": "sim", "action": "Stop"})
        out = f.follow()
        self.assertIn("!!! failed: tile requests: tiles: sim Start: ValueError", out.stdout)
        self.assertIn(f"SD_REPORT_ATTENTION: repo-sync on ", out.stdout)
        self.assertIn("follow: system already at the hub's pin", out.stdout)
        self.assertEqual(f.runs(), 0)

    def test_a_line_the_file_cannot_read_is_flagged_not_silent(self):
        """NEW (review round 1). A line parse refuses is a failure named
        with its line, never a silent success; the lines it read are
        still answered."""
        f = self.fixture()
        f.conf(f"bogus\ntile|work|sim|Simulator|{f.app}|-\nact|sim|Start|true\n")
        f.ask()
        out = f.follow()
        self.assertIn("!!! failed: tile requests: tiles: place-tiles.conf: line 1: expected tile|", out.stdout)
        self.assertIn("SD_REPORT_ATTENTION: repo-sync on ", out.stdout)
        self.assertEqual(f.answer()["state"], "running")

    def test_the_documented_line_with_a_host_runs_here_and_another_hosts_is_left(self):
        """NEW (review round 1). The line the README documents, with this
        machine as its <host>, is answered; a tile on another host is not
        this machine's to answer, and is no failure. Needs the pack with
        host lines (pack sd:6387): the pin after the pack PR, or
        SD_ACCEPTANCE_PACK naming that pack before it."""
        f = self.fixture()
        f.conf(f"tile|work|sim|Simulator|{f.app}|-|{HOST}\n"
               "act|sim|Start|sh -c \"echo ran >> runs\"\n"
               f"tile|work|far|Far|{f.app}|-|x{HOST}\nact|far|Start|sh -c \"echo far >> runs\"\n")
        f.ask()
        f.ask(tile="far", rid="cd34")
        out = f.follow()
        self.assertNotIn("!!!", out.stdout)
        self.assertEqual(f.answer()["state"], "running")
        f.ended()
        self.assertEqual(f.runs(), 1)
        self.assertIsNone(f.answer(tile="far"), "another host's tile was answered here")

    def test_no_file_here_asks_nothing(self):
        """NEW. A satellite with no place-tiles.conf does not reach the hub for
        tiles: no row is read or written, and follow says nothing of tiles."""
        f = self.fixture()
        (f.config / "project-dashboard" / "place-tiles.conf").unlink()
        f.ask()
        out = f.follow()
        self.assertNotIn("tiles:", out.stdout)
        self.assertIsNone(f.answer())


class TilesHookTest(unittest.TestCase):
    """follow's hook with satellite_tiles.py's stand-in (test_follow's stub)."""

    def fixture(self):
        f = RefreshFixture()
        self.addCleanup(f.destroy)
        return f

    def test_a_hub_that_does_not_answer_is_said_and_flags_nothing(self):
        """NEW. Exit 3 is the hub's silence: said, not flagged; the next run asks again."""
        f = self.fixture()
        out = f.run("follow", expect=0, extra_env={"SATELLITE_TILES_RC": "3"})
        self.assertIn("follow: tile requests not read: the hub did not answer", out.stdout)
        self.assertNotIn("SD_REPORT_ATTENTION", out.stdout)

    def test_a_failure_is_flagged_for_reports_and_follow_goes_on(self):
        """NEW. Exit 2 flags its stderr line, with an SD_REPORT_ATTENTION line, and follow still runs."""
        f = self.fixture()
        out = f.run("follow", expect=0, extra_env={"SATELLITE_TILES_RC": "2", "SATELLITE_TILES_ERR": "boom\n"})
        self.assertIn("!!! failed: tile requests: boom", out.stdout)
        self.assertIn("tile requests failed: boom", out.stdout)
        self.assertIn("follow: system already at the hub's pin", out.stdout)


if __name__ == "__main__":
    unittest.main()
