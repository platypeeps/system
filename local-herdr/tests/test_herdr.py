"""herdr.sh against the shell double in tests/doubles/.

Every test runs the real entrypoint as a subprocess with the double first on
PATH, an empty HOME, and HERDR_STATE_DIR in a temporary directory. Nothing
here ever reaches the installed herdr: the double is prepended to PATH and
the suite asserts that in test_double_is_the_herdr_on_path.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE.parent
SCRIPT = TOOL / "herdr.sh"
DOUBLES = HERE / "doubles"

RUNNING = {
    "sessions": [
        {
            "default": False,
            "name": "sd",
            "running": True,
            "session_dir": "/tmp/sd",
            "socket_path": "/tmp/sd/herdr.sock",
        }
    ]
}


def pane_info(
    pane: str, agent: str | None, session_id: str | None, cwd: str, foreground_cwd: str | None = None
) -> dict:
    info: dict = {"pane_id": pane, "cwd": cwd, "foreground_cwd": foreground_cwd or cwd}
    if agent:
        info["agent"] = agent
        info["agent_status"] = "idle"
        if session_id:
            info["agent_session"] = {
                "agent": agent,
                "kind": "id",
                "source": f"herdr:{agent}",
                "value": session_id,
            }
    return {"id": "cli:pane:get", "result": {"pane": info, "type": "pane_info"}}


class HerdrCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="sd-herdr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.state = self.tmp / "state"
        self.fixture = self.tmp / "fixture"
        self.journal = self.tmp / "journal.tsv"
        (self.fixture / "panes").mkdir(parents=True)
        self.home.mkdir()

    def env(self, **extra: str) -> dict:
        env = {
            "PATH": f"{DOUBLES}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "HERDR_STATE_DIR": str(self.state),
            "HERDR_DOUBLE_FIXTURE": str(self.fixture),
            "HERDR_DOUBLE_JOURNAL": str(self.journal),
            "TMPDIR": str(self.tmp),
        }
        for key in ("PYTHON", "LANG", "LC_ALL"):
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(extra)
        return env

    def run_sh(self, *args: str, cwd: str | None = None, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            cwd=cwd or str(self.tmp),
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def journal_lines(self) -> list[list[str]]:
        if not self.journal.exists():
            return []
        return [line.split("\t") for line in self.journal.read_text().splitlines()]

    def herdr_calls(self) -> list[list[str]]:
        """Journal lines without the leading HERDR_SESSION and socket columns."""
        return [line[2:] for line in self.journal_lines()]

    def session_running(self) -> None:
        (self.fixture / "session-list.json").write_text(json.dumps(RUNNING))

    def pane(
        self, pane: str, agent: str | None, session_id: str | None, cwd: str,
        foreground_cwd: str | None = None,
    ) -> None:
        (self.fixture / "panes" / f"{pane.replace(':', '_')}.json").write_text(
            json.dumps(pane_info(pane, agent, session_id, cwd, foreground_cwd))
        )

    def foreground(self, pane: str, name: str) -> None:
        """Put a process other than the shell in the pane's foreground."""
        (self.fixture / "panes" / f"{pane.replace(':', '_')}.process.json").write_text(json.dumps({
            "id": "cli:pane:process_info",
            "result": {"type": "pane_process_info", "process_info": {
                "pane_id": pane, "shell_pid": 100, "foreground_process_group_id": 200,
                "tty": "/dev/ttys009",
                "foreground_processes": [{"pid": 200, "name": name, "argv": [name]}],
            }},
        }))

    def state_file(self) -> dict:
        return json.loads((self.state / "sd.json").read_text())


class TestHelpAndUsage(HerdrCase):
    def test_no_args_prints_usage_to_stderr_and_exits_1(self) -> None:
        proc = self.run_sh()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("usage: herdr.sh", proc.stderr)

    def test_help_exits_0_and_declares_convention_6(self) -> None:
        for flag in ("help", "-h", "--help"):
            with self.subTest(flag=flag):
                proc = self.run_sh(flag)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage: herdr.sh", proc.stdout)
                # local-health-check finds convention-6 tools by this phrase.
                self.assertIn("local-health-check", proc.stdout)

    def test_unknown_verb_exits_1(self) -> None:
        proc = self.run_sh("frobnicate")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("usage: herdr.sh", proc.stderr)

    def test_double_is_the_herdr_on_path(self) -> None:
        proc = subprocess.run(
            ["sh", "-c", "command -v herdr && herdr --version"],
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [str(DOUBLES / "herdr"), "herdr 0.9.0"])


class TestRecord(HerdrCase):
    def test_record_writes_the_documented_shape(self) -> None:
        proc = self.run_sh("record", "w1:p2", "claude", "abc-123", "/work/repo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.state_file()
        self.assertEqual(data["session"], "sd")
        self.assertEqual(len(data["panes"]), 1)
        entry = data["panes"][0]
        self.assertEqual(
            set(entry), {"pane", "agent", "session_id", "cwd", "recorded_at", "name"}
        )
        self.assertEqual(entry["pane"], "w1:p2")
        self.assertEqual(entry["agent"], "claude")
        self.assertEqual(entry["session_id"], "abc-123")
        self.assertEqual(entry["cwd"], "/work/repo")
        self.assertEqual(entry["name"], "sd-w1-p2")
        self.assertRegex(entry["recorded_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # Written pretty, one key per line: readable by a person.
        text = (self.state / "sd.json").read_text()
        self.assertIn('\n      "session_id": "abc-123"\n', text)
        # record never asks herdr anything.
        self.assertEqual(self.herdr_calls(), [])

    def test_record_defaults_cwd_to_the_callers(self) -> None:
        proc = self.run_sh("record", "w1:p2", "codex", "sid-9", cwd=str(self.home))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state_file()["panes"][0]["cwd"], str(self.home.resolve()))

    def test_record_replaces_the_same_pane_and_keeps_others(self) -> None:
        self.run_sh("record", "w1:p1", "claude", "one", "/a")
        self.run_sh("record", "w1:p2", "codex", "two", "/b")
        self.run_sh("record", "w1:p1", "claude", "three", "/c")
        panes = {p["pane"]: p for p in self.state_file()["panes"]}
        self.assertEqual(set(panes), {"w1:p1", "w1:p2"})
        self.assertEqual(panes["w1:p1"]["session_id"], "three")
        self.assertEqual(panes["w1:p2"]["session_id"], "two")

    def test_record_refuses_an_agent_without_a_resume_form(self) -> None:
        proc = self.run_sh("record", "w1:p1", "gemini", "x", "/a")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no known resume form", proc.stderr)
        self.assertFalse((self.state / "sd.json").exists())

    def test_session_name_selects_the_file(self) -> None:
        proc = self.run_sh("record", "w1:p1", "claude", "x", "/a", HERDR_SESSION="other")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.state / "other.json").exists())
        self.assertFalse((self.state / "sd.json").exists())

    def test_default_state_dir_is_xdg_state_home(self) -> None:
        env = self.env(XDG_STATE_HOME=str(self.tmp / "xdg"))
        del env["HERDR_STATE_DIR"]
        proc = subprocess.run(
            ["sh", str(SCRIPT), "record", "w1:p1", "claude", "x", "/a"],
            env=env, capture_output=True, text=True, check=False, cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.tmp / "xdg" / "sd-herdr" / "sd.json").exists())

    def test_list_and_forget(self) -> None:
        self.run_sh("record", "w1:p1", "claude", "one", "/a")
        self.run_sh("record", "w1:p2", "codex", "two", "/b")
        proc = self.run_sh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("w1:p1", proc.stdout)
        self.assertIn("two", proc.stdout)
        proc = self.run_sh("forget", "w1:p1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([p["pane"] for p in self.state_file()["panes"]], ["w1:p2"])
        proc = self.run_sh("forget", "w1:p1")
        self.assertEqual(proc.returncode, 1)

    def test_agent_names_stay_distinct_when_the_pane_id_is_folded(self) -> None:
        long_a = "w1:p" + "x" * 40 + "a"
        long_b = "w1:p" + "x" * 40 + "b"
        for pane in ("w1:pG", "w1:pg", long_a, long_b):
            proc = self.run_sh("record", pane, "claude", "id-" + pane, "/a")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        names = {p["pane"]: p["name"] for p in self.state_file()["panes"]}
        self.assertEqual(len(set(names.values())), 4, names)
        for name in names.values():
            self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")
        # An id that folds without loss keeps the short readable name.
        self.assertEqual(names["w1:pg"], "sd-w1-pg")


class TestConcurrency(HerdrCase):
    def test_concurrent_records_all_land(self) -> None:
        procs = [
            subprocess.Popen(
                ["sh", str(SCRIPT), "record", f"w1:p{i}", "claude", f"sid-{i}", "/a"],
                cwd=str(self.tmp), env=self.env(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(24)
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
        panes = {p["pane"] for p in self.state_file()["panes"]}
        self.assertEqual(panes, {f"w1:p{i}" for i in range(24)})
        self.assertEqual(sorted(os.listdir(self.state)), ["sd.json", "sd.json.lock"])

    def test_record_waits_for_the_state_lock(self) -> None:
        self.state.mkdir()
        with open(self.state / "sd.json.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = subprocess.Popen(
                ["sh", str(SCRIPT), "record", "w1:p1", "claude", "c-1", "/a"],
                cwd=str(self.tmp), env=self.env(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), "record wrote while another writer held the lock")
            fcntl.flock(held, fcntl.LOCK_UN)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual([p["pane"] for p in self.state_file()["panes"]], ["w1:p1"])

    def test_a_record_made_while_resume_runs_survives_it(self) -> None:
        # A resumed agent's SessionStart hook records its pane while
        # `resume` is still working; resume must not write its older copy
        # over that record, and must not hold the lock while herdr works.
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        # No panes/w1_p1.json: the pane is gone, so resume moves the record
        # to the recreated pane and has to write the file back.
        hook = self.fixture / "on-agent-start"
        hook.write_text(f"#!/bin/sh\nexec sh '{SCRIPT}' record w1:p5 codex x-5 /work/e\n")
        hook.chmod(0o755)
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        panes = {p["pane"]: p["session_id"] for p in self.state_file()["panes"]}
        self.assertEqual(panes, {"w9:p1": "c-1", "w1:p5": "x-5"})


class TestSnapshot(HerdrCase):
    def test_snapshot_records_every_agent_with_a_session_id(self) -> None:
        self.session_running()
        (self.fixture / "agent-list.json").write_text(json.dumps({
            "id": "cli:agent:list",
            "result": {"agents": [
                pane_info("w1:p1", "claude", "c-1", "/a")["result"]["pane"],
                pane_info("w1:p2", "codex", "x-2", "/b")["result"]["pane"],
                pane_info("w1:p3", "claude", None, "/c")["result"]["pane"],
                pane_info("w1:p4", "gemini", "g-4", "/d")["result"]["pane"],
            ], "type": "agent_list"},
        }))
        proc = self.run_sh("snapshot")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 agent(s)", proc.stdout)
        panes = {p["pane"]: p for p in self.state_file()["panes"]}
        self.assertEqual(set(panes), {"w1:p1", "w1:p2"})
        self.assertEqual(panes["w1:p2"]["agent"], "codex")
        self.assertEqual(panes["w1:p2"]["session_id"], "x-2")
        self.assertEqual(panes["w1:p2"]["cwd"], "/b")
        # Every herdr call went to the named session, and once `session list`
        # named its socket, to that socket -- not one inherited from the
        # pane the wrapper was run in.
        self.assertEqual({line[0] for line in self.journal_lines()}, {"sd"})
        self.assertEqual(
            [line[1] for line in self.journal_lines()], ["", "/tmp/sd/herdr.sock"]
        )

    def test_an_inherited_socket_path_is_not_passed_on(self) -> None:
        self.session_running()
        proc = self.run_sh("snapshot", HERDR_SOCKET_PATH="/other/session/herdr.sock")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("/other/session/herdr.sock", [line[1] for line in self.journal_lines()])

    def test_snapshot_without_a_running_session_fails(self) -> None:
        proc = self.run_sh("snapshot")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not running", proc.stderr)


class TestStart(HerdrCase):
    def test_start_creates_the_session_when_absent(self) -> None:
        proc = self.run_sh("start")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.herdr_calls()
        self.assertEqual(calls[0], ["session", "list", "--json"])
        self.assertEqual(calls[-1], ["--session", "sd"])
        self.assertNotIn(["agent", "list"], calls)

    def test_start_honours_HERDR_SESSION(self) -> None:
        proc = self.run_sh("start", HERDR_SESSION="work")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.journal_lines()[-1], ["work", "", "--session", "work"])

    def test_start_attaches_through_the_target_sessions_socket(self) -> None:
        self.session_running()
        proc = self.run_sh("start", HERDR_SOCKET_PATH="/other/session/herdr.sock")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.journal_lines()[-1], ["sd", "/tmp/sd/herdr.sock", "--session", "sd"])

    def test_start_resumes_each_recorded_pane_then_attaches(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.run_sh("record", "w1:p2", "codex", "x-2", "/work/b")
        self.journal.unlink(missing_ok=True)
        # Both panes came back as bare shells in their saved directories,
        # which is what herdr does for panes it could not restore natively.
        self.pane("w1:p1", None, None, "/work/a")
        self.pane("w1:p2", None, None, "/work/b")
        proc = self.run_sh("start")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.herdr_calls()
        self.assertIn(
            ["agent", "start", "sd-w1-p1", "--kind", "claude", "--pane", "w1:p1", "--", "--resume", "c-1"],
            calls,
        )
        self.assertIn(
            ["agent", "start", "sd-w1-p2", "--kind", "codex", "--pane", "w1:p2", "--", "resume", "x-2"],
            calls,
        )
        # No cd: the panes were already in their recorded directories.
        self.assertFalse(any(c[:2] == ["pane", "run"] for c in calls))
        self.assertEqual(calls[-1], ["--session", "sd"])
        self.assertIn("w1:p1: resumed (claude c-1)", proc.stdout)
        self.assertIn("w1:p2: resumed (codex x-2)", proc.stdout)

    def test_resume_changes_directory_first_when_the_pane_is_elsewhere(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/it's here")
        self.journal.unlink(missing_ok=True)
        self.pane("w1:p1", None, None, str(self.home))
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.herdr_calls()
        cd = calls.index(["pane", "run", "w1:p1", "cd '/work/it'\\''s here'"])
        start = calls.index(
            ["agent", "start", "sd-w1-p1", "--kind", "claude", "--pane", "w1:p1", "--", "--resume", "c-1"]
        )
        self.assertLess(cd, start)

    def test_resume_reads_the_foreground_directory_not_the_panes(self) -> None:
        # herdr's `cwd` is where the pane was opened; the shell has since
        # moved, and `foreground_cwd` is where it is now.
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.journal.unlink(missing_ok=True)
        self.pane("w1:p1", None, None, "/work/a", foreground_cwd="/elsewhere")
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(["pane", "run", "w1:p1", "cd '/work/a'"], self.herdr_calls())

    def test_resume_leaves_a_pane_running_an_ordinary_command_alone(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.journal.unlink(missing_ok=True)
        self.pane("w1:p1", None, None, "/elsewhere")
        self.foreground("w1:p1", "vim")
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("w1:p1: busy", proc.stdout)
        self.assertIn("vim", proc.stderr)
        calls = self.herdr_calls()
        self.assertIn(["pane", "process-info", "--pane", "w1:p1"], calls)
        self.assertFalse(any(c[:2] in (["agent", "start"], ["pane", "run"]) for c in calls))

    def test_resume_fails_one_pane_whose_process_info_errors_and_goes_on(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.run_sh("record", "w1:p2", "codex", "x-2", "/work/b")
        self.pane("w1:p1", None, None, "/work/a")
        self.pane("w1:p2", None, None, "/work/b")
        (self.fixture / "panes" / "w1_p1.process.json").write_text(json.dumps(
            {"error": {"code": "unsupported", "message": "no process info"}}
        ))
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("w1:p1: failed", proc.stdout)
        self.assertIn("w1:p2: resumed (codex x-2)", proc.stdout)

    def test_resume_skips_a_pane_that_already_holds_the_session(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.journal.unlink(missing_ok=True)
        self.pane("w1:p1", "claude", "c-1", "/work/a")
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("w1:p1: live", proc.stdout)
        self.assertFalse(any(c[:2] == ["agent", "start"] for c in self.herdr_calls()))

    def test_resume_leaves_a_pane_holding_another_agent_alone(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.journal.unlink(missing_ok=True)
        self.pane("w1:p1", "codex", "x-9", "/work/a")
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("w1:p1: busy", proc.stdout)
        self.assertIn("left alone", proc.stderr)
        self.assertFalse(any(c[:2] == ["agent", "start"] for c in self.herdr_calls()))

    def test_resume_recreates_a_pane_that_is_gone_and_records_the_new_id(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.journal.unlink(missing_ok=True)
        # No panes/w1_p1.json: the double answers pane_not_found.
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.herdr_calls()
        self.assertIn(["workspace", "create", "--cwd", "/work/a", "--no-focus"], calls)
        self.assertIn(
            ["agent", "start", "sd-w9-p1", "--kind", "claude", "--pane", "w9:p1", "--", "--resume", "c-1"],
            calls,
        )
        self.assertEqual(self.state_file()["panes"][0]["pane"], "w9:p1")
        self.assertEqual(self.state_file()["panes"][0]["session_id"], "c-1")

    def test_resume_reports_a_refused_start_and_exits_1(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.pane("w1:p1", None, None, "/work/a")
        (self.fixture / "agent-start-fail").write_text("")
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("w1:p1: failed", proc.stdout)
        self.assertIn("agent_not_ready", proc.stderr)

    def test_resume_without_a_state_file_does_nothing(self) -> None:
        self.session_running()
        proc = self.run_sh("resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to resume", proc.stdout)
        self.assertEqual(self.herdr_calls(), [])


class TestStatus(HerdrCase):
    def test_3_when_herdr_is_not_installed(self) -> None:
        env = self.env(PATH=str(self.tmp / "empty-bin"))
        (self.tmp / "empty-bin").mkdir()
        # sh and python3 still have to resolve: link the ones running the suite.
        for name in ("sh", "dirname", "python3"):
            real = shutil.which(name)
            self.assertIsNotNone(real, name)
            os.symlink(real, self.tmp / "empty-bin" / name)
        if "PYTHON" in env:
            del env["PYTHON"]
        self.state.mkdir()
        (self.state / "sd.json").write_text(json.dumps({"session": "sd", "panes": []}))
        proc = subprocess.run(
            ["sh", str(SCRIPT), "status"], env=env,
            capture_output=True, text=True, check=False, cwd=str(self.tmp),
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("not installed", proc.stdout)

    def test_3_when_there_is_no_state_file(self) -> None:
        self.session_running()
        proc = self.run_sh("status")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("no state file", proc.stdout)
        # Nothing to check means herdr was not even asked.
        self.assertEqual(self.herdr_calls(), [])

    def test_1_when_the_session_is_gone(self) -> None:
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        proc = self.run_sh("status")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not running", proc.stdout)

    def test_1_when_a_pane_is_gone_or_holds_something_else(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.run_sh("record", "w1:p2", "codex", "x-2", "/work/b")
        self.pane("w1:p1", "claude", "c-1", "/work/a")
        proc = self.run_sh("status")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("w1:p2: gone", proc.stdout)
        self.pane("w1:p2", "codex", "x-other", "/work/b")
        proc = self.run_sh("status")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("w1:p2: holds codex x-other, recorded codex x-2", proc.stdout)

    def test_0_when_every_recorded_pane_holds_its_session(self) -> None:
        self.session_running()
        self.run_sh("record", "w1:p1", "claude", "c-1", "/work/a")
        self.run_sh("record", "w1:p2", "codex", "x-2", "/work/b")
        self.pane("w1:p1", "claude", "c-1", "/work/a")
        self.pane("w1:p2", "codex", "x-2", "/work/b")
        proc = self.run_sh("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 recorded pane(s) live", proc.stdout)


if __name__ == "__main__":
    unittest.main()
