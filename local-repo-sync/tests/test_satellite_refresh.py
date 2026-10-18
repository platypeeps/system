"""Tests for Refresh satellites: follow answers the hub's request row (sd:5108).

The dashboard writes one `refresh.request` setting row in the hub's database.
A satellite's `repo-sync.sh follow`, its five-minute job, drains its lanes,
follows the hub's pins, runs `machine-setup.sh update --apply`, and writes the
result to `refresh.result.<host>`. The database is a real one at the pinned
pack's schema (`SD_ACCEPTANCE_PACK`, else `.ci/pack`), opened through
`REPO_SYNC_SD_DB`; the hub is never reached. The checkouts, lanes and
machine-setup are `test_follow.py`'s fixtures.

Each case names its kind: NEW was written before follow answered a request,
and seen to fail against the script without it; PIN keeps a decision.
"""

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_follow import PROOF, FollowFixture  # noqa: E402
from test_refresh_migrate import source_pack  # noqa: E402

FOLDER = pathlib.Path(__file__).resolve().parent.parent

# machine-setup.sh for these cases: test_follow's, except that a full
# `update --apply` is allowed. It logs its arguments, runs FULL_HOOK, prints
# FULL_LINE and exits FULL_RC. The bin and satellite dry runs print their
# proof lines, as the real stages do.
SETUP_STUB = ('printf \'%s\\n\' "$*" >> "$UPDATE_LOG"\n'
              'if [ "$2" = --apply ]; then\n'
              '  [ -z "$FULL_HOOK" ] || eval "$FULL_HOOK"\n'
              '  echo "== cron"; echo "${FULL_LINE:-done: 0 failures}"; exit "${FULL_RC:-0}"\n'
              'fi\n'
              'if [ "$3" != --apply ]; then case "$2" in\n'
              f'  bin) echo "{PROOF["bin"]}" ;;\n'
              f'  satellite) echo "{PROOF["satellite"]}" ;;\n'
              'esac; fi\n')

HOST = socket.gethostname().split(".")[0].lower()

# satellite_refresh.py that always has request ab12 pending, so a case can fail
# one write: `start` exits START_RC and `finish` FINISH_RC. It logs its verbs.
WRITES_STUB = ('import os, sys\n'
               'open(os.environ["UPDATE_LOG"] + ".ask", "a").write(sys.argv[1] + "\\n")\n'
               'if sys.argv[1] == "check":\n'
               '    print("pending ab12 1"); sys.exit(0)\n'
               'sys.stderr.write("satellite_refresh.py " + sys.argv[1] + ": HubUnreachable: the hub did not answer\\n")\n'
               'sys.exit(int(os.environ.get(sys.argv[1].upper() + "_RC", "0")))\n')


class RefreshFixture(FollowFixture):
    """A satellite at the hub's pins, with the real satellite_refresh.py and a real database."""

    def __init__(self):
        super().__init__()
        shutil.copy(FOLDER / "satellite_refresh.py", self.folder / "satellite_refresh.py")
        shutil.copytree(source_pack() / "lib", self.sd_pack / "lib",
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"))
        # The script runs `python3`; this run's interpreter has what sd_db needs.
        (self.bin / "python3").symlink_to(sys.executable)
        self.satellite()
        self.system = self.system_repo()
        self.commit(self.system, "local-machine-setup/machine-setup.sh", SETUP_STUB, "full update stub")
        self.git(self.system, "push", "-q", "origin", "main")
        self.pack = self.pack_repo()
        for repo in (self.system, self.pack):
            self.pin(repo)
        self.set_hub_pin(self.system, self.head(self.system), pack=self.head(self.pack))
        self.call("import sd_db; sd_db.initialise(db)")

    def call(self, code, **values):
        """Runs `code` against the fixture database with the pack's sd_db; prints what it prints."""
        script = ("import json, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]); "
                  "import satellite_refresh as sr; from sd_db import connect, writes; db = sys.argv[3]; "
                  "values = json.loads(sys.argv[4])\n" + code)
        out = subprocess.run([sys.executable, "-c", script, str(self.sd_pack / "lib"), str(self.folder),
                              str(self.database), json.dumps(values)],
                             capture_output=True, text=True, check=False)
        if out.returncode:
            raise AssertionError(out.stderr)
        return out.stdout

    def setting(self, key):
        """The row's JSON, or None when the database holds none."""
        text = self.call("held = writes.read_settings(connect(db), [values['key']]); "
                         "print(json.dumps(held.get(values['key'])))", key=key)
        value = json.loads(text)
        return None if value is None else json.loads(value)

    def put(self, key, value):
        self.call("writes.set_setting(connect(db), values['key'], values['value'])", key=key,
                  value=value if isinstance(value, str) else json.dumps(value))

    def ask(self):
        """A dashboard request; returns its id."""
        return json.loads(self.call("print(json.dumps(sr.request(connect(db))))"))["id"]

    def result(self):
        return self.setting(self.call("print(sr.host_key(sr.this_host()))").strip())

    def full_updates(self):
        return [call for call in self.update_calls() if call == "update --apply"]


class RefreshTest(unittest.TestCase):
    def fixture(self):
        f = RefreshFixture()
        self.addCleanup(f.destroy)
        return f

    def test_a_request_runs_machine_setup_and_writes_the_result(self):
        """NEW. A pending request drains, follows, runs the full machine-setup
        update once and writes `done` with its exit, step and last line. The
        next run finds it answered and runs nothing."""
        f = self.fixture()
        wanted = f.ask()

        result = f.run("follow", expect=0, extra_env={"FULL_LINE": "summary: 0 failed"})

        self.assertIn("lanes held", result.stdout)
        self.assertEqual(["update --apply"], f.full_updates())
        row = f.result()
        self.assertEqual((wanted, "done", 0, "machine-setup", "summary: 0 failed", 1, HOST),
                         (row["request"], row["state"], row["exit"], row["step"], row["line"], row["attempt"],
                          row["host"]))
        self.assertTrue(row["started"] <= row["finished"])

        again = f.run("follow", expect=0)
        self.assertEqual(["update --apply"], f.full_updates())
        self.assertNotIn("lanes held", again.stdout)

    def test_a_failed_machine_setup_writes_its_failure(self):
        """NEW. machine-setup's failure is the result, with its exit and last
        line, never a success; follow exits with it."""
        f = self.fixture()
        f.ask()

        f.run("follow", expect=1, extra_env={"FULL_RC": "1", "FULL_LINE": "FAILED brew: 2 items"})

        row = f.result()
        self.assertEqual(("done", 1, "machine-setup", "FAILED brew: 2 items"),
                         (row["state"], row["exit"], row["step"], row["line"]))

    def test_a_failed_follow_is_the_result_and_runs_no_machine_setup(self):
        """NEW. A follow that refuses answers the request with its own
        failure line; machine-setup does not run on a pair it did not reach."""
        f = self.fixture()
        f.set_hub_pin(f.system, f.advance_origin(f.system), pack=f.head(f.pack))
        (f.system / "README").write_text("edited\n")
        f.ask()

        f.run("follow", expect=1)

        row = f.result()
        self.assertEqual(("done", 1, "follow"), (row["state"], row["exit"], row["step"]))
        self.assertIn("uncommitted changes", row["line"])
        self.assertEqual([], f.full_updates())

    def test_a_run_stopped_between_start_and_finish_runs_again_once(self):
        """NEW. Steps: a run killed during machine-setup leaves the row
        `running`. The next run finds it, runs the update again as attempt 2,
        and writes `done`."""
        f = self.fixture()
        wanted = f.ask()
        # machine-setup's parent is st_bounded's perl, in a group of its own;
        # above it are bound_step's subshell and the shell that runs the
        # request, the drain's child. KILL to both, as a job limit ends that
        # group, and no trap runs. The suite's own group is not signalled.
        kill = ('p2=$(ps -o ppid= -p "$PPID" | tr -d " "); p3=$(ps -o ppid= -p "$p2" | tr -d " "); '
                'kill -KILL "$p3" "$p2"')

        f.run("follow", expect=137, extra_env={"FULL_HOOK": kill})
        stopped = f.result()
        self.assertEqual((wanted, "running", 1, None), (stopped["request"], stopped["state"],
                                                       stopped["attempt"], stopped["exit"]))

        f.run("follow", expect=0)
        row = f.result()
        self.assertEqual(("done", 2, 0), (row["state"], row["attempt"], row["exit"]))
        self.assertEqual(["update --apply", "update --apply"], f.full_updates())

    def test_a_second_stopped_attempt_is_written_failed_and_not_run_again(self):
        """NEW. A request whose second attempt also stopped is written as
        failed, with the commands to run by hand, and runs nothing more."""
        f = self.fixture()
        wanted = f.ask()
        f.put(f.call("print(sr.host_key(sr.this_host()))").strip(),
              {"host": HOST, "request": wanted, "attempt": 2, "state": "running", "started": "2026-10-18T20:00:00+00:00"})

        f.run("follow", expect=0)

        row = f.result()
        self.assertEqual(("done", 1, "interrupted"), (row["state"], row["exit"], row["step"]))
        self.assertIn("machine-setup.sh update --apply", row["line"])
        self.assertEqual([], f.full_updates())

    def asked(self, f):
        return pathlib.Path(str(f.update_log) + ".ask").read_text().split()

    def test_a_failed_start_runs_nothing_and_exits_1(self):
        """NEW. Failures: a start that was not written runs no update, names
        the failure and exits 1; the next run asks again."""
        f = self.fixture()
        (f.folder / "satellite_refresh.py").write_text(WRITES_STUB)

        result = f.run("follow", expect=1, extra_env={"START_RC": "3"})

        self.assertIn("refresh request ab12 not started", result.stdout)
        self.assertEqual([], f.full_updates())
        self.assertEqual(["check", "check", "start"], self.asked(f))

    def test_a_failed_finish_exits_1_and_says_the_row_still_reads_running(self):
        """NEW. Failures: a result that was not written is not a success. The
        run exits 1 and names it; the row still reads running, so the next
        run runs the request again as attempt 2."""
        f = self.fixture()
        (f.folder / "satellite_refresh.py").write_text(WRITES_STUB)

        result = f.run("follow", expect=1, extra_env={"FINISH_RC": "3"})

        self.assertIn("refresh request ab12: machine-setup exited 0, not recorded", result.stdout)
        self.assertIn("the row still reads running", result.stdout)
        self.assertEqual(["update --apply"], f.full_updates())
        self.assertEqual(["check", "check", "start", "finish"], self.asked(f))

    def test_a_new_request_while_one_was_answered_runs_again(self):
        """NEW. A request with a new id is pending again, whatever the last result said."""
        f = self.fixture()
        f.ask()
        f.run("follow", expect=0)
        wanted = f.ask()

        f.run("follow", expect=0)

        self.assertEqual(wanted, f.result()["request"])
        self.assertEqual(["update --apply", "update --apply"], f.full_updates())

    def test_no_request_runs_nothing_and_writes_nothing(self):
        """NEW. With no request, follow runs as before, drains nothing, and
        writes no row: the dashboard lists the satellite from its job's run
        reports, not from a row the check writes."""
        f = self.fixture()

        result = f.run("follow", expect=0)

        self.assertNotIn("lanes held", result.stdout)
        self.assertEqual([], f.full_updates())
        self.assertIsNone(f.result())
        self.assertEqual("0", f.call("print(connect(db).execute(\"SELECT count(*) FROM state WHERE kind='setting' AND key LIKE 'refresh.%'\").fetchone()[0])").strip())

    def test_an_unreadable_request_fails_and_runs_nothing(self):
        """NEW. Failures: a request row that does not read is unknown, not
        "nothing pending": follow exits 1, names it, and runs no update."""
        f = self.fixture()
        f.put("refresh.request", "not json")

        result = f.run("follow", expect=1)

        self.assertIn("refresh request not read", result.stdout)
        self.assertEqual([], f.full_updates())
        self.assertIsNone(f.result())

    def test_a_hub_that_does_not_answer_still_follows(self):
        """PIN. A hub that does not answer is skipped, as the satellite stage
        skips it: follow runs, exits with its own status, and says so."""
        f = self.fixture()
        (f.folder / "satellite_refresh.py").write_text("import sys\nsys.exit(3)\n")

        result = f.run("follow", expect=0)

        self.assertIn("the hub did not answer", result.stdout)
        self.assertNotIn("lanes held", result.stdout)

    def test_the_hub_answers_no_request(self):
        """PIN. The hub holds the request; its own follow does nothing with it."""
        f = self.fixture()
        (f.tmp / "home" / ".config" / "sd" / "hub.json").unlink()
        f.ask()

        result = f.run("follow", expect=0)

        self.assertIn("this machine is the hub", result.stdout)
        self.assertEqual([], f.full_updates())

    def test_document_reads_each_satellite_state(self):
        """NEW. The dashboard's read: the request, and each host whose
        repo-sync-follow job reported a run (the newest run per host that
        cron-jobs records), as never, pending, running or done, with that
        run's end; a row that does not read is unread. The reading host and
        other jobs are not listed."""
        f = self.fixture()
        for key in [f"cron-chain:repo-sync-follow@{h}" for h in ("gamma", "alpha", "delta", "beta-2", HOST)] + [
                "cron-chain:nightly-sync@omega", "cron-report:repo-sync-follow"]:
            f.call("writes.record_state(connect(db), 'heartbeat', key=values['key'], "
                   "body={'run_id': 'r1', 'ended': '2026-10-18T20:05:00Z', 'item': None})", key=key)
        wanted = f.ask()
        f.put("refresh.result.beta_2", {"request": "old", "state": "done", "exit": 0})
        f.put("refresh.result.gamma", {"request": wanted, "state": "running", "attempt": 1})
        f.put("refresh.result.delta", "[1]")

        doc = json.loads(f.call("print(json.dumps(sr.document(connect(db))))"))

        self.assertEqual(wanted, doc["request"]["id"])
        self.assertEqual([("alpha", "never"), ("beta-2", "pending"), ("delta", "unread"), ("gamma", "running")],
                         [(row["host"], row["state"]) for row in doc["satellites"]])
        self.assertEqual({"2026-10-18T20:05:00Z"}, {row["followed"] for row in doc["satellites"]})

    def test_host_keys_are_setting_keys(self):
        """NEW. A host name with a hyphen or a leading digit still makes a valid setting key."""
        f = self.fixture()
        keys = json.loads(f.call("print(json.dumps([sr.host_key(h) for h in values['hosts']]))",
                                 hosts=["sat-1", "9box", "Sat"]))
        self.assertEqual(["refresh.result.sat_1", "refresh.result.h9box", "refresh.result.sat"], keys)
        f.call("from sd_db import writes; [writes._setting_key(k) for k in values['keys']]", keys=keys)

    def test_a_result_row_another_host_wrote_is_refused(self):
        """NEW. Evidence: the row names the host that wrote it. Two hosts
        whose names map to one key fail the check, never answer for each other."""
        f = self.fixture()
        wanted = f.ask()
        out = f.call("c = connect(db); writes.set_setting(c, sr.host_key('sat-1'), json.dumps({'host': 'sat-1', "
                     "'request': values['id'], 'state': 'done', 'exit': 0}))\n"
                     "try:\n    sr.check(c, 'sat_1')\nexcept ValueError as e:\n    print(e)", id=wanted)
        self.assertIn("holds sat-1's result, not sat_1's", out)

    def test_exit_codes_name_the_hub_failures(self):
        """NEW. A hub that does not answer exits 3 and a refused build 4, so
        follow can tell them from a failure, which exits 2."""
        f = self.fixture()
        codes = json.loads(f.call(
            "from sd_db import remote; "
            "print(json.dumps([sr._exit_for(remote.HubUnreachable('hub.example.test', 8769, 'x')), sr._exit_for(remote.BuildMismatch('build', 'a', 'b')), "
            "sr._exit_for(ValueError('x'))]))"))
        self.assertEqual([3, 4, 2], codes)


if __name__ == "__main__":
    unittest.main()
