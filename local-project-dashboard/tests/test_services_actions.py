"""Real HTTP service controls with disposable plists and a fake launchctl."""

import os
import plistlib
import subprocess
from pathlib import Path

from sd_db import services
from sd_dashboard.runtime import LABEL as DASHBOARD_LABEL

from .test_workflow_actions import BrowserSession


class ServicesActions(BrowserSession):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name)
        self.agents = self.root / "LaunchAgents"
        self.daemons = self.root / "LaunchDaemons"
        self.agents.mkdir()
        self.daemons.mkdir()
        self.program = self.root / "fixture-service"
        self.program.write_text("#!/bin/sh\n")
        self.program.chmod(0o755)
        self.domain = f"gui/{os.getuid()}"
        self.label = "com.example.fixture-service"
        self.configs, self.observations, self.calls = {}, {}, []
        self.add_service(self.label)
        self.add_service(self.label, system=True)
        self.add_service(DASHBOARD_LABEL)
        self.add_service("com.example.scheduled", scheduled=True)
        self.services_backend = services.ServiceBackend(user_agents=self.agents,
            system_daemons=self.daemons, uid=os.getuid(), runner=self.launchctl)
        self.listening.RequestHandlerClass.services_backend = self.services_backend

    def add_service(self, label, *, system=False, scheduled=False):
        config = {"Label": label, "ProgramArguments": [str(self.program), "--secret", "HIDDEN"],
                  "RunAtLoad": True, "KeepAlive": True}
        if scheduled:
            config.pop("KeepAlive")
            config["StartCalendarInterval"] = {"Hour": 9, "Minute": 0}
        folder = self.daemons if system else self.agents
        (folder / (label + ".plist")).write_bytes(plistlib.dumps(config))
        service = ("system" if system else self.domain) + "/" + label
        self.configs[service] = config
        self.observations[service] = ["running", 111, 0, 2]

    def launchctl(self, argv):
        self.calls.append(argv)
        if argv[1] == "print-disabled":
            return subprocess.CompletedProcess(argv, 0, "\tdisabled services = {\n\t}\n", "")
        if argv[1] == "print":
            service = argv[2]
            if service not in self.observations:
                return subprocess.CompletedProcess(argv, 113, "", "SECRET diagnostic")
            # A fifth element is launchd's signal line after a run that ended in a
            # signal; a code of None omits the exit-code line, as launchd does then.
            state, pid, code, runs, *rest = self.observations[service]
            config = self.configs[service]
            folder = self.daemons if service.startswith("system/") else self.agents
            lines = [service + " = {", "\tpath = " + str(folder / (config["Label"] + ".plist")),
                     "\tprogram = " + str(self.program), "\tstate = " + state,
                     "\truns = " + str(runs)]
            if code is not None:
                lines.append("\tlast exit code = " + str(code))
            if rest:
                lines.append("\tlast terminating signal = " + rest[0])
            if pid:
                lines.append("\tpid = " + str(pid))
            lines += ["\targuments = {", *("\t\t" + value for value in config["ProgramArguments"]),
                      "\t}", "\tenvironment = {", "\t\tSECRET = HIDDEN", "\t}", "}"]
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines), "")
        if argv[1] == "bootout":
            self.observations.pop(argv[2], None)
        elif argv[1] == "bootstrap":
            config = plistlib.loads(Path(argv[3]).read_bytes())
            service = argv[2] + "/" + config["Label"]
            self.observations[service] = ["not running", None, 0, 0]
        elif argv[1] == "kickstart":
            self.observations[argv[2]] = ["running", 222, 0, 1]
        else:
            self.fail(f"Unexpected launchctl command: {argv}")
        return subprocess.CompletedProcess(argv, 0, "HIDDEN", "")

    def actions(self):
        return [call for call in self.calls if call[1] not in ("print", "print-disabled")]

    def state(self, label=None):
        return services.service_state(self.connection, label or self.label, backend=self.services_backend)

    def test_services_show_safe_state_scope_and_only_supported_controls(self):
        before = self.snapshot()
        status, _, page = self.request("/operations?area=services")
        self.assertEqual(status, 200)
        for text in ("Launchd services", "User LaunchAgent", "System daemon", "Running", "PID 111",
                     "Stop unloads", "next login", f"sd services stop {self.label}"):
            self.assertIn(text, page)
        self.assertEqual(page.count(f'action="/api/services/{self.label}/stop"'), 1)
        self.assertNotIn(f'action="/api/services/{DASHBOARD_LABEL}/', page)
        self.assertNotIn('action="/api/services/com.example.scheduled/', page)
        for secret in ("SECRET", "HIDDEN", "--secret", "ProgramArguments"):
            self.assertNotIn(secret, page)
        self.assertIn('name="area" value="services"', page)
        self.assertNotIn("Age in status", page)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.actions(), [])
        filtered = self.request("/operations?area=services&q=daemon")[2]
        self.assertIn("System daemon", filtered)
        self.assertNotIn("User LaunchAgent ·", filtered)
        self.assertNotIn('action="/api/services/', filtered)

    def test_signal_ended_service_renders_its_signal_and_keeps_controls(self):
        """A SIGTERM'd, restarted service has no exit code; the card names the
        signal and the service stays controllable instead of reading unknown (sd:1331)."""
        self.observations[self.domain + "/" + self.label] = ["running", 31900, None, 6, "Terminated: 15"]
        status, _, page = self.request("/operations?area=services")
        self.assertEqual(status, 200)
        card = page[page.index(self.label):]
        self.assertIn("Last signal SIGTERM (15)", card)
        self.assertIn("PID 31900", card)
        self.assertNotIn("Last exit", card[:card.index("</article>")])
        self.assertNotIn("Unknown", card[:card.index("</article>")])
        self.assertEqual(page.count(f'action="/api/services/{self.label}/stop"'), 1)
        # A stopped service that died of a crash signal is Failed on the card,
        # named by signal, and still startable.
        self.observations[self.domain + "/" + self.label] = ["not running", None, None, 7, "Segmentation fault: 11"]
        page = self.request("/operations?area=services")[2]
        card = page[page.index(self.label):]
        card = card[:card.index("</article>")]
        self.assertIn("Failed", card)
        self.assertIn("Last signal SIGSEGV (11)", card)
        self.assertNotIn("Last exit", card)
        self.assertEqual(page.count(f'action="/api/services/{self.label}/start"'), 1)
        self.assertEqual(self.actions(), [])

    def test_stop_start_and_restart_use_exact_installed_service_and_revision(self):
        files = {path: path.read_bytes() for path in self.root.rglob("*.plist")}
        initial = self.state()
        status, _, stopped = self.post(f"/api/services/{self.label}/stop", {"revision": initial["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(stopped["request"]["status"], "accepted")
        self.assertEqual(stopped["service"]["state"], "unloaded")
        self.assertEqual([argv[1:] for argv in self.actions()], [["bootout", self.domain + "/" + self.label]])
        before = self.snapshot()
        self.assertEqual(self.post(f"/api/services/{self.label}/stop", {"revision": initial["revision"]})[0], 409)
        self.assertEqual(self.snapshot(), before)
        status, _, started = self.post(f"/api/services/{self.label}/start", {"revision": stopped["service"]["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(started["request"]["status"], "accepted")
        expected_start = [["bootstrap", self.domain, str(self.agents / (self.label + ".plist"))],
                          ["kickstart", self.domain + "/" + self.label]]
        self.assertEqual([argv[1:] for argv in self.actions()][1:], expected_start)
        status, _, restarted = self.post(f"/api/services/{self.label}/restart", {"revision": started["service"]["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(restarted["request"]["status"], "accepted")
        self.assertEqual([argv[1:] for argv in self.actions()][3:],
                         [["bootout", self.domain + "/" + self.label], *expected_start])
        page = self.request("/operations?area=services")[2]
        self.assertIn("Last request: ", page)
        self.assertIn("accepted", page)
        self.assertIn("not proof of application health", page)
        self.assertEqual(files, {path: path.read_bytes() for path in self.root.rglob("*.plist")})

    def test_auth_forgery_and_unsupported_targets_have_no_side_effects(self):
        state = self.state()
        before = self.snapshot()
        for path, payload in (
            (f"/api/services/{self.label}/stop", {"revision": state["revision"], "command": "custom"}),
            (f"/api/services/{self.label}/stop", {}),
            (f"/api/services/{self.label}/stop", {"revision": "broken"}),
            (f"/api/services/{self.label}/enable", {"revision": state["revision"]}),
            (f"/api/services/system:{self.label}/stop", {"revision": state["revision"]}),
            ("/api/services/com.unknown.fixture/stop", {"revision": state["revision"]}),
        ):
            self.assertIn(self.post(path, payload)[0], (400, 404))
        for label in (DASHBOARD_LABEL, "com.example.scheduled"):
            self.assertEqual(self.post(f"/api/services/{label}/stop", {"revision": self.state(label)["revision"]})[0], 400)
        for headers in ({"Origin": "https://other.invalid"}, {"Cookie": ""}, {"X-SD-CSRF": "0" * 64}):
            self.assertEqual(self.post(f"/api/services/{self.label}/stop", {"revision": state["revision"]}, **headers)[0], 403)
        self.assertEqual(self.request(f"/api/services/{self.label}/stop")[0], 404)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.actions(), [])
