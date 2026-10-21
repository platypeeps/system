"""Standard setup (sd:5610): Management runs `sd fleet standard` in the registered checkout and answers its plan.

What this slice promises: the preview runs the verb with `--json` and no `--apply`, in the checkout the repo row names,
against the database the dashboard serves. The apply posts the preview's digest, and only a 64-hex digest reaches the
verb. A refusal (exit 3, or argparse's 2 from a pack without the verb) is 409 and says nothing ran; a plan with a
failed step is 200 with the step's result; no plan, or none within the bound, is 502 and says to Check again, since
the verb may have written. A stub stands in for `sd`: no test reaches GitHub or the live database.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from sd_dashboard import standard
from sd_db import upsert_repo
from support import ScreenCase
from test_workflow_actions import BrowserSession

STUB = """#!/bin/sh
pwd > "{cwd}"
printf '%s\\n' "$@" > "{log}"
cat "{answer}"
echo "{said}" >&2
exit $(cat "{code}")
"""
DIGEST = "a" * 64


def plan(**over) -> dict:
    return {"path": "", "slug": "example/checkout", "branch": "main", "private": True, "owned": True, "managed": False,
            "used_before": False, "digest": DIGEST, "applied": False,
            "steps": [{"key": "G4", "group": "github", "subject": "Delete head branches", "state": "change",
                       "detail": "", "before": False, "after": True}], **over}


class Stub(ScreenCase):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        upsert_repo(self.connection, str(self.checkout), remote="git@example.invalid:example/checkout.git")
        stub = root / "sd"
        self.cwd, self.log, self.answer, self.code = root / "cwd", root / "argv", root / "answer", root / "code"
        stub.write_text(STUB.format(cwd=self.cwd, log=self.log, answer=self.answer, code=self.code,
                                    said="sd fleet standard: a ship is running; try again"))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.verb(plan())
        patcher = patch("sd_dashboard.queue_screen.command", lambda name: str(stub))
        patcher.start()
        self.addCleanup(patcher.stop)

    def verb(self, answer, code: int = 0) -> None:
        self.answer.write_text(answer if isinstance(answer, str) else json.dumps(answer))
        self.code.write_text(str(code))

    def sent(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


class TheModule(Stub):
    def test_the_preview_runs_the_verb_read_only_in_the_rows_checkout_on_this_database(self):
        status, got = standard.preview(self.connection, {"path": [str(self.checkout)]})
        self.assertEqual((status, got["digest"]), (200, DIGEST))
        self.assertEqual(self.sent()[:4], ["fleet", "standard", "--json", "--database"])
        self.assertEqual(Path(self.sent()[4]).resolve(), self.path.resolve())
        self.assertEqual(len(self.sent()), 5)
        self.assertEqual(Path(self.cwd.read_text().strip()).resolve(), self.checkout)

    def test_an_unregistered_checkout_runs_nothing(self):
        status, got = standard.preview(self.connection, {"path": [str(self.checkout.parent)]})
        self.assertEqual(status, 409)
        self.assertIn("not a registered repository", got["error"])
        self.assertEqual(self.sent(), [])
        self.assertEqual(standard.preview(self.connection, {})[0], 400)

    def test_the_apply_takes_only_a_hex_digest_and_hands_it_to_the_verb(self):
        for bad in ({"path": str(self.checkout)}, {"path": str(self.checkout), "digest": "--apply"},
                    {"path": str(self.checkout), "digest": DIGEST, "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                standard.request(bad)
        self.verb(plan(applied=True))
        status, got = standard.request({"path": str(self.checkout), "digest": DIGEST})(self.connection)
        self.assertEqual((status, got["applied"]), (200, True))
        self.assertEqual(self.sent()[-3:], ["--apply", "--digest", DIGEST])

    def test_a_refusal_is_409_with_the_verbs_reason_and_a_changed_plan_rides_along(self):
        self.verb(plan(digest="b" * 64), code=3)
        status, got = standard.request({"path": str(self.checkout), "digest": DIGEST})(self.connection)
        self.assertEqual(status, 409)
        self.assertEqual(got["error"], "sd fleet standard: a ship is running; try again")
        self.assertEqual(got["plan"]["digest"], "b" * 64)
        self.verb("", code=2)
        self.assertEqual(standard.preview(self.connection, {"path": str(self.checkout)})[0], 409)

    def test_a_failed_step_is_a_plan_and_no_plan_is_unknown(self):
        failed = plan(steps=[{"key": "G4", "state": "change", "result": "failed: HTTP 403"}])
        self.verb(failed, code=1)
        status, got = standard.request({"path": str(self.checkout), "digest": DIGEST})(self.connection)
        self.assertEqual((status, got["steps"][0]["result"]), (200, "failed: HTTP 403"))
        self.verb("Traceback (most recent call last):", code=1)
        status, got = standard.request({"path": str(self.checkout), "digest": DIGEST})(self.connection)
        self.assertEqual((status, got["outcome"]), (502, "unknown"))
        self.assertIn("Check again", got["error"])

    def test_a_run_past_the_bound_is_unknown(self):
        with patch("sd_dashboard.standard.subprocess.run", side_effect=subprocess.TimeoutExpired("sd", 1)):
            status, got = standard.request({"path": str(self.checkout), "digest": DIGEST})(self.connection)
        self.assertEqual((status, got["outcome"]), (502, "unknown"))


class Route(Stub, BrowserSession):
    def test_the_page_previews_with_a_get_and_applies_with_a_post(self):
        status, _, body = self.request(f"/api/repos/standard/preview?path={quote(str(self.checkout))}",
                                       headers={"Cookie": self.cookie})
        self.assertEqual((status, json.loads(body)["digest"]), (200, DIGEST), body)
        self.assertNotIn("--apply", self.sent())
        status, _, got = self.post("/api/repos/standard", {"path": str(self.checkout), "digest": DIGEST})
        self.assertEqual(status, 200, got)
        self.assertIn("--apply", self.sent())
        self.assertEqual(self.post("/api/repos/standard", {"path": str(self.checkout), "digest": "x"})[0], 400)
