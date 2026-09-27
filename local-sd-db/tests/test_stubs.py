"""The six commands, on PATH rather than patched over."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sd_db.testing import STUB_NAMES, ProviderDouble, Reply, Stubs


class StubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stubs = Stubs(self.root / "stubs")
        self.environment = self.stubs.environment(base={"PATH": "/usr/bin:/bin"})

    def run_stub(self, name, *args, stdin=None, expect=0):
        completed = subprocess.run(
            [str(self.stubs.bin / name), *args],
            # `input=""` and not an inherited stdin: a stub given a pipe with
            # no writer would wait for one that never closes.
            capture_output=True, text=True, input=stdin or "", env=self.environment,
        )
        self.assertEqual(completed.returncode, expect, completed.stderr)
        return completed


class TheInstallation(StubCase):
    def test_every_named_command_is_installed_and_first_on_path(self):
        for name in STUB_NAMES:
            self.assertTrue((self.stubs.bin / name).exists(), name)
        self.assertEqual(self.environment["PATH"].split(":")[0], str(self.stubs.bin))

    def test_an_unknown_name_is_refused_rather_than_silently_missing(self):
        with self.assertRaises(ValueError):
            Stubs(self.root / "other", names=("rsync",))


class TheLaunchctlStub(StubCase):
    def test_bootstrap_then_list_shows_the_job(self):
        plist = self.root / "com.sd.runner.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        self.run_stub("launchctl", "bootstrap", "gui/501", str(plist))
        listing = self.run_stub("launchctl", "list").stdout
        self.assertIn("com.sd.runner", listing)

    def test_bootout_removes_it_and_a_second_bootout_fails(self):
        plist = self.root / "com.sd.runner.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        self.run_stub("launchctl", "bootstrap", "gui/501", str(plist))
        self.run_stub("launchctl", "bootout", "gui/501/com.sd.runner")
        self.run_stub("launchctl", "bootout", "gui/501/com.sd.runner", expect=113)

    def test_print_reports_the_state_the_test_set(self):
        self.stubs.state("launchctl", {"jobs": {"com.sd.runner": {"pid": 7, "status": 2, "state": "waiting"}}})
        out = self.run_stub("launchctl", "print", "gui/501/com.sd.runner").stdout
        self.assertIn("state = waiting", out)
        self.assertIn("last exit code = 2", out)

    def test_an_unknown_label_exits_113(self):
        self.run_stub("launchctl", "list", "com.sd.absent", expect=113)

    def test_the_calls_are_recorded_in_order(self):
        self.run_stub("launchctl", "list")
        self.run_stub("launchctl", "list", "com.sd.absent", expect=113)
        calls = self.stubs.calls("launchctl")
        self.assertEqual([call.argv for call in calls], [["list"], ["list", "com.sd.absent"]])
        self.assertEqual([call.status for call in calls], [0, 113])


class TheTailscaleStub(StubCase):
    def test_status_json_reports_the_addresses_the_test_set(self):
        self.stubs.state("tailscale", {"ips": ["100.64.0.9"], "hostname": "laptop"})
        payload = json.loads(self.run_stub("tailscale", "status", "--json").stdout)
        self.assertEqual(payload["Self"]["TailscaleIPs"], ["100.64.0.9"])
        self.assertEqual(payload["Self"]["HostName"], "laptop")

    def test_a_stopped_backend_fails_the_way_the_real_one_does(self):
        self.stubs.state("tailscale", {"backend": "Stopped"})
        completed = self.run_stub("tailscale", "status", expect=1)
        self.assertIn("stopped", completed.stderr.lower())


class TheCurlStub(StubCase):
    def test_it_performs_the_request_so_the_double_sees_it(self):
        with ProviderDouble() as double:
            double.queue("kimi", Reply(text="reached"))
            url = double.base_url("kimi") + "/chat/completions"
            out = self.run_stub("curl", "-s", "-X", "POST", "-d", '{"messages":[]}', url).stdout
            self.assertIn("reached", out)
            self.assertEqual(len(double.calls_to("kimi")), 1)

    def test_the_status_code_is_available_through_write_out(self):
        with ProviderDouble() as double:
            url = double.base_url("kimi") + "/embeddings"
            out = self.run_stub(
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", "-d", "{}", url
            ).stdout
            self.assertEqual(out, "404")

    def test_a_refused_connection_exits_seven(self):
        self.run_stub("curl", "-s", "http://127.0.0.1:1/nothing", expect=7)

    def test_the_body_is_recorded(self):
        with ProviderDouble() as double:
            url = double.base_url("kimi") + "/chat/completions"
            self.run_stub("curl", "-s", "-d", '{"model":"kimi-k3"}', url)
        self.assertIn("kimi-k3", self.stubs.calls("curl")[-1].stdin)


class TheCaffeinateStub(StubCase):
    def test_it_runs_the_command_it_wraps(self):
        out = self.run_stub("caffeinate", "-i", "/bin/echo", "ran").stdout
        self.assertEqual(out.strip(), "ran")

    def test_a_failing_command_stays_failing(self):
        self.run_stub("caffeinate", "-i", "/bin/sh", "-c", "exit 4", expect=4)
        self.assertEqual(self.stubs.calls("caffeinate")[-1].status, 4)


class TheLsofStub(StubCase):
    def test_an_unowned_port_exits_one(self):
        self.run_stub("lsof", "-i", ":52415", expect=1)

    def test_an_owned_port_reports_the_owner(self):
        self.stubs.state("lsof", {"ports": {"52415": {"pid": 900, "command": "exo"}}})
        out = self.run_stub("lsof", "-i", ":52415").stdout
        self.assertIn("52415", out)
        self.assertIn("exo", out)
        self.assertEqual(self.run_stub("lsof", "-t", "-i", ":52415").stdout.strip(), "900")


class TheLocalNotifyStub(StubCase):
    def test_the_notification_is_recorded_and_nothing_is_displayed(self):
        self.run_stub("local-notify", "--title", "sd", "the queue is stuck", stdin="")
        call = self.stubs.calls("local-notify")[-1]
        self.assertEqual(call.argv, ["--title", "sd", "the queue is stuck"])
        self.assertEqual(call.status, 0)


if __name__ == "__main__":
    unittest.main()
