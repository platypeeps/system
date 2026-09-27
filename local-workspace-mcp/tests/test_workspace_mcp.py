"""workspace-mcp.sh against the stubs in tests/doubles/.

Every test runs the real entrypoint as a subprocess with the doubles first on
PATH, an empty HOME and a temporary state directory. The stub launchctl and
curl journal every call, so nothing here reaches launchd or port 8083:
test_doubles_are_first_on_path asserts that.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE.parent
SCRIPT = TOOL / "workspace-mcp.sh"
DOUBLES = HERE / "doubles"


def max_time(call: str) -> int:
    return int(call.split("--max-time ", 1)[1].split()[0])


class WorkspaceMcpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="workspace-mcp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        (self.tmp / "home").mkdir()
        self.state = self.tmp / "state"

    def given(self, *, loaded: bool = True, curl: str = "ok", heal: bool = False,
              notify_rc: int = 0) -> None:
        if loaded:
            (self.fake / "loaded").touch()
        (self.fake / "curl_mode").write_text(curl + "\n")
        if heal:
            (self.fake / "heal_on_kickstart").touch()
        (self.fake / "notify_rc").write_text(f"{notify_rc}\n")

    def run_sh(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{DOUBLES}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.tmp / "home"),
            "TMPDIR": str(self.tmp),
            "FAKE_DIR": str(self.fake),
            "WORKSPACE_MCP_STATE_DIR": str(self.state),
            "WORKSPACE_MCP_NOTIFY": str(DOUBLES / "notify.sh"),
            "WORKSPACE_MCP_GRACE": "2",
            "WORKSPACE_MCP_RESTART_WAIT": "2",
            "WORKSPACE_MCP_POLL": "1",
            "WORKSPACE_MCP_NOTIFY_TIMEOUT": "2",
            **extra,
        }
        # 20s: well under a stalled notifier's 30s, so a notifier that is
        # not bounded, or a child of it left holding stdout, times out here.
        return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True,
                              text=True, timeout=20)

    def journal(self, prefix: str) -> list[str]:
        path = self.fake / "journal"
        if not path.exists():
            return []
        return [ln for ln in path.read_text().splitlines() if ln.startswith(prefix + " ")]


class HelpTests(WorkspaceMcpCase):
    def test_help_exits_zero_and_declares_the_sweep_sentence(self) -> None:
        for arg in ("help", "-h", "--help"):
            r = self.run_sh(arg)
            self.assertEqual(r.returncode, 0, arg)
            self.assertIn("local-health-check reads these codes", r.stdout)

    def test_no_argument_prints_usage_to_stderr_and_exits_one(self) -> None:
        r = self.run_sh()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("usage: workspace-mcp.sh", r.stderr)

    def test_doubles_are_first_on_path(self) -> None:
        self.given()
        self.run_sh("status")
        self.assertTrue(self.journal("launchctl"))
        self.assertTrue(self.journal("curl"))


class StatusTests(WorkspaceMcpCase):
    def test_healthy_server_exits_zero(self) -> None:
        self.given()
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("healthy", r.stdout)

    def test_probe_is_an_initialize_bounded_by_ten_seconds(self) -> None:
        self.given()
        self.run_sh("status")
        (call,) = [c for c in self.journal("curl") if '"method":"initialize"' in c]
        self.assertIn("--max-time 10", call)
        self.assertIn('"method":"initialize"', call)
        self.assertIn("http://127.0.0.1:8083/mcp", call)

    def test_probe_completes_the_handshake_and_closes_its_session(self) -> None:
        self.given()
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.journal("curl")
        (note,) = [c for c in calls if "notifications/initialized" in c]
        self.assertIn("mcp-session-id: abc123", note)
        self.assertIn(max_time(note), (9, 10))
        (close,) = [c for c in calls if "-X DELETE" in c]
        self.assertIn("mcp-session-id: abc123", close)
        self.assertIn(max_time(close), (9, 10))
        self.assertLess(calls.index(note), calls.index(close))

    def test_the_handshake_gets_only_the_time_initialize_left(self) -> None:
        # Three calls with a full TIMEOUT each could take 3 x TIMEOUT, past
        # local-health-check's 30s bound. They share one deadline instead.
        self.given()
        (self.fake / "curl_delay").write_text("2\n")
        r = self.run_sh("status", WORKSPACE_MCP_TIMEOUT="4")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.journal("curl")
        (note,) = [c for c in calls if "notifications/initialized" in c]
        (close,) = [c for c in calls if "-X DELETE" in c]
        self.assertLessEqual(max_time(note), 2)
        self.assertLessEqual(max_time(close), 2)

    def test_no_time_left_after_initialize_is_broken(self) -> None:
        self.given()
        (self.fake / "curl_delay").write_text("3\n")
        r = self.run_sh("status", WORKSPACE_MCP_TIMEOUT="3")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("left no time within 3s", r.stdout)
        self.assertEqual([c for c in self.journal("curl") if "initialize" not in c or "notifications" in c], [])

    def test_a_session_close_that_fails_is_broken(self) -> None:
        # A close that fails leaves one session behind per probe; the probe
        # says so instead of reporting healthy.
        for answer, shown in (("500", "HTTP 500"), ("fail", "curl exit 7")):
            with self.subTest(answer=answer):
                self.given()
                (self.fake / "delete_code").write_text(answer + "\n")
                r = self.run_sh("status")
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("did not close its session", r.stdout)
                self.assertIn(shown, r.stdout)

    def test_a_server_that_refuses_session_close_is_healthy(self) -> None:
        # The MCP spec lets a server answer 405 to DELETE when clients may
        # not end sessions; that is not a fault.
        self.given()
        (self.fake / "delete_code").write_text("405\n")
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_close_skipped_for_lack_of_time_says_why(self) -> None:
        # The bound wins over the cleanup: the probe stays healthy, sends no
        # DELETE, and names the session it left open.
        self.given()
        (self.fake / "curl_delay").write_text("1\n")
        (self.fake / "note_delay").write_text("2\n")
        r = self.run_sh("status", WORKSPACE_MCP_TIMEOUT="3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("left its session open: no time within 3s to close it", r.stdout)
        self.assertEqual([c for c in self.journal("curl") if "-X DELETE" in c], [])

    def test_agent_not_loaded_is_nothing_to_check(self) -> None:
        self.given(loaded=False)
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("not loaded", r.stdout)
        self.assertEqual(self.journal("curl"), [])

    def test_loaded_but_unanswered_is_broken(self) -> None:
        self.given(curl="hang")
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("curl exit 28", r.stdout)

    def test_http_error_is_broken(self) -> None:
        self.given(curl="500")
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 500", r.stdout)

    def test_answer_without_session_id_is_broken(self) -> None:
        self.given(curl="nosid")
        self.assertEqual(self.run_sh("status").returncode, 1)

    def test_status_never_restarts(self) -> None:
        self.given(curl="hang")
        self.run_sh("status")
        self.assertEqual([c for c in self.journal("launchctl") if "kickstart" in c], [])


class WatchTests(WorkspaceMcpCase):
    def kickstarts(self) -> list[str]:
        return [c for c in self.journal("launchctl") if "kickstart" in c]

    def test_healthy_does_not_restart(self) -> None:
        self.given()
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.kickstarts(), [])

    def test_a_failed_close_is_reported_but_never_restarts(self) -> None:
        # The server answers the handshake; only the DELETE fails. A
        # kickstart -k would drop every live session on every run.
        self.given()
        (self.fake / "delete_code").write_text("500\n")
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("did not close its session (curl exit 0, HTTP 500)", r.stdout)
        self.assertEqual(self.kickstarts(), [])

    def test_a_failed_close_in_the_grace_window_never_restarts(self) -> None:
        self.given()
        (self.fake / "curl_seq").write_text("hang\nok\n")
        (self.fake / "delete_code").write_text("500\n")
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("did not close its session", r.stdout)
        self.assertEqual(self.kickstarts(), [])

    def test_not_loaded_does_not_restart(self) -> None:
        self.given(loaded=False, curl="hang")
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.kickstarts(), [])

    def test_hung_server_is_kickstarted_once_and_recovers(self) -> None:
        self.given(curl="hang", heal=True)
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (kick,) = self.kickstarts()
        self.assertRegex(kick, r"^launchctl kickstart -k gui/\d+/local\.system-tools\.google-workspace-mcp$")
        self.assertIn("recovered", r.stdout)
        self.assertEqual(self.journal("notify"), [])

    def test_the_label_prefix_comes_from_the_shared_variable(self) -> None:
        self.given(curl="hang", heal=True)
        r = self.run_sh("watch", SYSTEM_TOOLS_LABEL_PREFIX="test.prefix")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (kick,) = self.kickstarts()
        self.assertRegex(kick, r"^launchctl kickstart -k gui/\d+/test\.prefix\.google-workspace-mcp$")

    def test_one_failed_probe_then_an_answer_does_not_restart(self) -> None:
        # A server still starting under launchd (about 90s) or stalled for
        # one probe answers within the grace window; a kickstart -k there
        # would reset its startup and drop every live session.
        self.given(curl="ok")
        (self.fake / "curl_seq").write_text("hang\n")
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.kickstarts(), [])
        self.assertEqual(self.journal("notify"), [])
        self.assertIn("answered within the grace window", r.stdout)

    def test_agent_unloaded_during_grace_window_is_not_restarted(self) -> None:
        # local-maintenance boots the agent out for a uv prune; a kickstart
        # then would reload an agent paused on purpose.
        self.given(curl="hang")
        (self.fake / "print_seq").write_text("loaded\ngone\n")
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.kickstarts(), [])
        self.assertEqual(self.journal("notify"), [])
        self.assertIn("not loaded", r.stdout)

    def test_agent_unloaded_after_restart_is_not_notified(self) -> None:
        # first probe and both grace probes see it loaded, then it is gone
        self.given(curl="hang")
        (self.fake / "print_seq").write_text("loaded\nloaded\nloaded\ngone\n")
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.kickstarts()), 1)
        self.assertEqual(self.journal("notify"), [])
        self.assertIn("not loaded", r.stdout)

    def test_restart_that_does_not_help_notifies_once_per_outage(self) -> None:
        self.given(curl="hang")
        first = self.run_sh("watch")
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertEqual(len(self.kickstarts()), 1)
        (note,) = self.journal("notify")
        self.assertIn("-c local,ntfy", note)
        self.assertIn("-p high", note)
        self.assertNotIn("email", note.split(" -b ")[0])

        second = self.run_sh("watch")
        self.assertEqual(second.returncode, 1)
        self.assertEqual(len(self.kickstarts()), 2)
        self.assertEqual(len(self.journal("notify")), 1)
        self.assertIn("already notified", second.stdout)

    def test_wait_after_restart_is_bounded(self) -> None:
        self.given(curl="hang")
        self.run_sh("watch")
        # one probe, GRACE / POLL = 2 in the grace window, then
        # RESTART_WAIT / POLL = 2 after the restart
        self.assertEqual(len(self.journal("curl")), 5)

    def test_recovery_clears_the_outage_marker(self) -> None:
        self.given(curl="hang")
        self.run_sh("watch")
        (self.fake / "curl_mode").write_text("ok\n")
        self.assertEqual(self.run_sh("watch").returncode, 0)
        (self.fake / "curl_mode").write_text("hang\n")
        self.run_sh("watch")
        self.assertEqual(len(self.journal("notify")), 2)

    def test_stalled_notifier_is_killed_and_retried_next_run(self) -> None:
        self.given(curl="hang")
        (self.fake / "notify_stall").touch()
        r = self.run_sh("watch")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("notify did not finish within 2s", r.stdout)
        for pid in (self.fake / "notify_pids").read_text().split():
            with self.assertRaises(ProcessLookupError, msg=f"pid {pid} survived"):
                os.kill(int(pid), 0)
        self.assertFalse((self.state / "alerted").exists())
        (self.fake / "notify_stall").unlink()
        self.run_sh("watch")
        self.assertEqual(len(self.journal("notify")), 2)
        self.assertTrue((self.state / "alerted").exists())

    def test_a_cancelled_watchdog_does_not_mark_a_timeout(self) -> None:
        # The watchdog's sleep dies by a signal before the notifier ends, as
        # when notify_bounded cancels it. A killed sleep is no deadline: the
        # notifier must finish and the outage count as notified.
        self.given(curl="hang")
        (self.fake / "sleep_killed").write_text("2\n")
        (self.fake / "notify_delay").write_text("1\n")
        r = self.run_sh("watch")
        self.assertNotIn("notify did not finish", r.stdout)
        self.assertTrue((self.state / "alerted").exists(), r.stdout + r.stderr)

    def test_failed_notify_is_retried_next_run(self) -> None:
        self.given(curl="hang", notify_rc=1)
        self.run_sh("watch")
        self.run_sh("watch")
        self.assertEqual(len(self.journal("notify")), 2)


if __name__ == "__main__":
    unittest.main()
