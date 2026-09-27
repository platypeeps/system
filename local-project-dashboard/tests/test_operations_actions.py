"""HTTP operations proof using an in-memory observer; no launchctl calls."""

import subprocess
import re

from sd_db import operations, record_state, workflow
from sd_db.workflow import WorkflowError

from .test_workflow_actions import BrowserSession


class FakeBackend:
    def __init__(self):
        self.states = {"failed-fixture": "failed", "running-fixture": "running", "idle-fixture": "idle",
                       "interrupted-fixture": "interrupted", "crashed-fixture": "failed"}
        # A last run that ended in a signal has no exit code; launchd records the
        # signal instead (sd:1344). SIGSEGV is the kernel's verdict, so that job
        # is failed; SIGKILL is a stop nobody here sent, so that one is
        # interrupted. Both carry the reason the parser writes.
        self.signals = {"interrupted-fixture": 9, "crashed-fixture": 11}
        # The launchd lifetime a job's run counter belongs to; a reload moves it.
        self.lifetimes = {}
        self.calls = []

    def names(self):
        return list(self.states)

    def inspect(self, name):
        if name not in self.states:
            raise WorkflowError("Unknown fixture job")
        state, signal = self.states[name], self.signals.get(name)
        return {"name": name, "label": "fixture." + name, "service": "fixture/" + name,
                "schedule": [{"Hour": 9, "Minute": 30}], "state": state,
                "pid": 123 if state == "running" else None,
                "last_exit": None if signal is not None else 1 if state == "failed" else 0,
                "last_signal": signal, "runs": 1, "lifetime": self.lifetimes.get(name, 47763), "boot": 1789568182,
                "reason": {9: "last run ended in SIGKILL (9)", 11: "last run ended in SIGSEGV (11)",
                           15: "last run ended in SIGTERM (15)"}.get(signal, ""),
                "_observation": name + state}

    def perform(self, action, service):
        self.calls.append((action, service))
        return subprocess.CompletedProcess([], 0, "", "")


class OperationsActions(BrowserSession):
    def setUp(self):
        self.backend = FakeBackend()
        super().setUp()

    def test_page_renders_observed_state_and_only_supported_controls(self):
        task = workflow.capture_task(self.connection, title="Delegated fixture", who="operator")["item"]["id"]
        queued = self.assignment(task, status="queued")
        running = self.assignment(task, status="running")
        unassigned = self.assignment(None, status="queued")
        before = self.snapshot()
        status, _, page = self.request("/operations")
        self.assertEqual(status, 200)
        self.assertIn('action="/api/jobs/failed-fixture/retry"', page)
        self.assertIn('action="/api/jobs/running-fixture/cancel"', page)
        visible_sections = page.split('<details>', 1)[0]
        self.assertIn('action="/api/jobs/running-fixture/cancel"', visible_sections)
        self.assertNotIn('action="/api/jobs/idle-fixture/retry"', page)
        self.assertIn(f'action="/api/runner/{queued}/cancel"', page)
        self.assertNotIn(f'action="/api/assignments/{running}/cancel"', page)
        self.assertIn("no supported cancellation backend", page)
        self.assertNotIn("prompt", page)
        self.assertIn(f"Assignment #{unassigned}", page)
        self.assertNotIn("/item/None", page)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])

    def test_signal_ended_job_renders_its_signal_next_to_last_exit(self):
        """A job whose last run ended in a signal has no exit code; the card names
        the signal. A crashed job and a SIGKILL'd one need attention with retry
        open and stop refused for the usual reason, not for the signal. Only a
        run this dashboard's own accepted cancel ended reads Idle, among the
        other jobs (sd:1344)."""
        status, _, page = self.request("/operations")
        self.assertEqual(status, 200)

        def card(page, name):
            start = page.index(name.replace("-", " "))
            return page[start:page.index("</article>", start)]

        attention = page.split("<details>", 1)[0]
        crashed = card(page, "crashed-fixture")
        self.assertIn("Failed", crashed)
        self.assertIn("Last signal SIGSEGV (11)", crashed)
        self.assertNotIn("Last exit", crashed)
        self.assertIn('action="/api/jobs/crashed-fixture/retry"', attention)
        self.assertIn("Stop job: only a currently running job can cancel", crashed)
        self.assertNotIn("last run ended", crashed)
        interrupted = card(page, "interrupted-fixture")
        self.assertIn("Interrupted", interrupted)
        self.assertIn("Last signal SIGKILL (9)", interrupted)
        self.assertNotIn("Last exit", interrupted)
        self.assertIn('action="/api/jobs/interrupted-fixture/retry"', attention)
        self.assertIn("Stop job: only a currently running job can cancel", interrupted)
        self.assertIn("Last exit 1", card(page, "failed-fixture"))
        self.assertNotIn("Last signal", card(page, "failed-fixture"))
        self.assertEqual(self.backend.calls, [])
        # Cancel the running job from here, then observe launchd's SIGTERM line
        # on that same run: the stop was meant, so the job is Idle, out of the
        # attention list and without a retry.
        state = operations.job_state(self.connection, "running-fixture", backend=self.backend)
        self.assertEqual(self.post("/api/jobs/running-fixture/cancel", {"revision": state["revision"]})[0], 200)
        self.backend.states["running-fixture"], self.backend.signals["running-fixture"] = "interrupted", 15
        page = self.request("/operations")[2]
        cancelled = card(page, "running-fixture")
        self.assertIn("Idle", cancelled)
        self.assertIn("Last signal SIGTERM (15)", cancelled)
        self.assertNotIn("running fixture", page.split("<details>", 1)[0])
        self.assertNotIn('action="/api/jobs/running-fixture/', page)
        # Reload the label (a new lifetime, runs = 1 again) and observe an
        # external SIGTERM on the new run 1: the old cancel is no evidence for
        # it, so the job is Interrupted and back in attention with its retry.
        self.backend.lifetimes["running-fixture"] = 47790
        page = self.request("/operations")[2]
        self.assertIn("Interrupted", card(page, "running-fixture"))
        self.assertIn('action="/api/jobs/running-fixture/retry"', page.split("<details>", 1)[0])
        self.assertEqual(self.backend.calls, [("cancel", "fixture/running-fixture")])

    def test_operations_tabs_select_one_area_and_default_to_jobs(self):
        ports_calls = []

        def ports_fixture():
            ports_calls.append("observed")
            return {"services": [{"name": "fixture-container", "ports": ["9010"],
                                   "state": "running", "mark": "+"}],
                    "listeners": {"9010": {"state": "listening", "source": "lsof"}},
                    "complete": True}

        self.listening.RequestHandlerClass.ports_backend = staticmethod(ports_fixture)
        for area, heading in (("jobs", "Running jobs"), ("progress", "Age in status"),
                              ("usage", "Week, cost and provider details"), ("ports", "fixture-container")):
            with self.subTest(area=area):
                status, _, page = self.request(f"/operations?area={area}")
                self.assertEqual(status, 200)
                navigation = re.search(r'<nav[^>]*aria-label="Operations areas"[^>]*>(.*?)</nav>', page).group(1)
                for key, label in (("jobs", "Jobs"), ("services", "Services"), ("ports", "Ports"),
                                   ("progress", "Progress"), ("usage", "Usage")):
                    self.assertIn(f'href="/operations?area={key}"', navigation)
                    self.assertIn(f">{label}</a>", navigation)
                active = re.findall(r'<a[^>]*href="([^"]+)"[^>]*aria-current="page"', navigation)
                self.assertEqual(active, [f"/operations?area={area}"])
                self.assertIn(heading, page)
                for other in ("Running jobs", "Age in status", "Week, cost and provider details", "fixture-container"):
                    if other != heading:
                        self.assertNotIn(other, page)
        self.assertEqual(ports_calls, ["observed"])
        for suffix in ("", "?area=unknown"):
            self.assertIn("Running jobs", self.request("/operations" + suffix)[2])
        self.assertEqual(self.backend.calls, [])

    def test_retry_acceptance_is_distinct_from_observed_job_state(self):
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        status, _, result = self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertEqual(result["job"]["state"], "failed")
        self.assertEqual(self.backend.calls, [("retry", "fixture/failed-fixture")])
        before = self.snapshot()
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})[0], 409)
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": result["job"]["revision"]})[0], 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.backend.calls), 1)
        page = self.request("/operations")[2]
        self.assertIn("Last request: ", page)
        self.assertIn("accepted", page)
        self.assertIn("current state is observed separately", page)

    def test_restore_blocks_retry_but_running_cancel_remains_available(self):
        record_state(self.connection, "restore", key="fixture-restore")
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})[0], 400)
        running = operations.job_state(self.connection, "running-fixture", backend=self.backend)
        self.assertEqual(self.post("/api/jobs/running-fixture/cancel", {"revision": running["revision"]})[0], 200)
        self.assertEqual(self.backend.calls, [("cancel", "fixture/running-fixture")])

    def test_queued_cancel_leaves_item_status_unchanged_and_stale_retry_refuses(self):
        task = workflow.capture_task(self.connection, title="Assignment target", who="operator")["item"]["id"]
        queued = self.assignment(task, status="queued")
        state = operations.assignment_state(self.connection, queued)
        status, _, result = self.post(f"/api/assignments/{queued}/cancel", {"revision": state["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(workflow.item_state(self.connection, task)["item"]["status"], "planning")
        before = self.snapshot()
        self.assertEqual(self.post(f"/api/assignments/{queued}/cancel", {"revision": state["revision"]})[0], 409)
        self.assertEqual(self.snapshot(), before)

    def test_forged_actions_and_cross_origin_requests_dispatch_nothing(self):
        before = self.snapshot()
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        for path, payload in (
            ("/api/jobs/failed-fixture/retry", {"revision": state["revision"], "command": "custom"}),
            ("/api/jobs/unknown/retry", {"revision": state["revision"]}),
            ("/api/jobs/failed-fixture/retry", {}),
            ("/api/jobs/failed-fixture/run", {"revision": state["revision"]}),
        ):
            self.assertIn(self.post(path, payload)[0], (400, 404))
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]},
                                  Origin="https://other.invalid")[0], 403)
        self.assertEqual(self.request("/api/jobs/failed-fixture/retry")[0], 404)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])
