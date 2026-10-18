"""Adversarial review (sd:5064): a task, a brief, a repository or a change, reviewed by codex in the background.

Every test puts a `codex` and an `sd` of its own first on PATH: the codex double writes a fixed result to the `-o`
path, or fails, or holds until released, and journals its argv and stdin; the sd double journals its argv and writes
nothing. The real `local-adversarial-gate/adversarial-gate.sh` runs between them, with its Jev seam pointed at a path
that does not exist, so no test reaches a model, the network, the judgment ledger or a database other than the
fixture's. `test_the_doubles_are_what_the_run_finds` asserts which `codex` and `sd` the server's PATH resolves.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import adversarial

from test_workflow_actions import BrowserSession

#: The longest a run against the doubles may take before the case fails naming it.
SETTLE = 30
RESULT = """# Adversarial review

## 1. The done condition cannot fail

> "Make the thing work."

The item names no check. **What would have to be true:** no test exists. Confidence: **LIKELY**

## 2. Scope hides a second repository

> "and the dashboard"

Two repos move. **What would have to be true:** the page lives elsewhere. Confidence: **SPECULATIVE**

## 3. `codex` may refuse the model

> "gpt-6-astra"

**What would have to be true:** the id is not served. Confidence: **CERTAIN**

## 4. A fourth one

> "x"

Confidence: **SPECULATIVE**

Does the central conclusion survive hostile reading? NO.
"""
CODEX = r"""#!/bin/sh
printf '%s\n' "$@" > "$CODEX_ARGV"
cat > "$CODEX_STDIN"
out=""
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && out="$2"
  shift
done
mode=$(cat "$CODEX_MODE" 2>/dev/null || echo ok)
if [ "$mode" = hold ]; then
  n=0
  while [ ! -f "$CODEX_RELEASE" ] && [ "$n" -lt 300 ]; do sleep 0.1; n=$((n + 1)); done
fi
if [ "$mode" = fail ]; then
  echo "error: model gpt-6-astra is not served to this account" >&2
  exit 3
fi
cp "$CODEX_RESULT" "$out"
"""
SD = r"""#!/bin/sh
{ printf '%s\0' "$@"; printf '\n'; } >> "$SD_JOURNAL"
[ ! -f "$SD_FAIL" ] || { echo "sd: item is locked" >&2; exit 1; }
"""


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                           *args], capture_output=True, text=True, check=True, timeout=30).stdout.strip()


class AdversarialCase(BrowserSession):
    def setUp(self):
        super().setUp()
        # Resolved, as git names a checkout: macOS's temporary folder is a symlink.
        root = Path(self.tmp.name).resolve()
        self.root = root
        self.bin, self.work, self.state = root / "bin", root / "checkout", root / "state"
        self.bin.mkdir()
        self.work.mkdir()
        for name, body in (("codex", CODEX), ("sd", SD)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.result_fixture = root / "result.md"
        self.result_fixture.write_text(RESULT)
        self.argv, self.stdin, self.mode = root / "codex.argv", root / "codex.stdin", root / "codex.mode"
        self.release, self.journal, self.sd_fail = root / "codex.release", root / "sd.journal", root / "sd.fail"
        self.config = root / "config"
        environment = patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", "XDG_STATE_HOME": str(self.state),
            "SYSTEM_TOOLS_CONFIG": str(self.config), "ADVERSARIAL_GATE_JEV": str(root / "no-jev"),
            "CODEX_ARGV": str(self.argv), "CODEX_STDIN": str(self.stdin), "CODEX_MODE": str(self.mode),
            "CODEX_RELEASE": str(self.release), "CODEX_RESULT": str(self.result_fixture),
            "SD_JOURNAL": str(self.journal), "SD_FAIL": str(self.sd_fail)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("ADVERSARIAL_MODEL", "ADVERSARIAL_EFFORT", "JEV_ADVERSARIAL_GATE"):
            os.environ.pop(name, None)
        git(self.work, "init", "-q", "-b", "main")
        (self.work / "README.md").write_text("# fixture\n")
        git(self.work, "add", "README.md")
        git(self.work, "commit", "-q", "-m", "first")
        self.key = self.repo(str(self.work))
        # A run still holding the lock when a case ends would hold it into the next case's state folder: release it.
        self.addCleanup(self.release.write_text, "")

    def review(self, kind, target, **extra):
        return self.post("/api/adversarial", {"kind": kind, "target": target, **extra})

    def read(self, run_id):
        status, _, body = self.request(f"/api/adversarial/{run_id}", headers={"Cookie": self.cookie})
        return status, json.loads(body)

    def settled(self, run_id):
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline:
            status, document = self.read(run_id)
            self.assertEqual(status, 200, document)
            if document["state"] in ("done", "failed") and (document["item"] is None or document["note"]):
                return document
            time.sleep(0.1)
        self.fail(f"run {run_id} did not settle in {SETTLE} s: {document}")

    def notes(self):
        if not self.journal.exists():
            return []
        return [line.rstrip("\0").split("\0") for line in self.journal.read_text().splitlines() if line]

    def codex_argv(self):
        return self.argv.read_text().splitlines()

    def task(self, title="Make the thing work", **columns):
        columns.setdefault("repo", self.key)
        return self.item(title, kind="task", **columns)


class TheTaskReview(AdversarialCase):
    def test_the_doubles_are_what_the_run_finds(self):
        self.assertEqual(Path(shutil.which("codex")).parent, self.bin)
        self.assertEqual(Path(shutil.which("sd")).parent, self.bin)

    def test_a_task_runs_codex_read_only_at_the_default_model_and_effort_and_notes_the_top_hypotheses(self):
        design = self.work / "docs" / "work" / "plan" / "design.md"
        design.parent.mkdir(parents=True)
        design.write_text("# plan\n")
        item = self.task(body={"text": "Make it, and the dashboard."}, path="docs/work/plan/design.md")
        self.note(item, "Ruling: keep it small.", kind="decision")
        before = self.snapshot()
        status, _, run = self.review("task", item)
        self.assertEqual(status, 202, run)
        self.assertRegex(run["id"], rf"^task-{item}-[0-9]{{8}}T[0-9]{{6}}Z$")
        self.assertEqual(run["state"], "queued")
        result = self.work / "docs" / "dashboard" / "adversarial" / f"{run['id']}.md"
        self.assertEqual(run["result"], str(result))
        self.assertEqual((run["model"], run["effort"]), ("gpt-6-astra", "high"))
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["reason"], done["note"]), ("done", None, "written"), done)
        self.assertEqual(result.read_text(), RESULT)
        self.assertEqual(self.codex_argv(), ["exec", "-m", "gpt-6-astra", "-c", "model_reasoning_effort=high",
                                             "-s", "read-only", "-C", str(self.work), "-o", str(result), "-"])
        prompt = self.stdin.read_text()
        self.assertIn("The document is a work item", prompt)
        self.assertIn(f"`{run['input']}`", prompt)
        self.assertIn(f"`{design}`", prompt)
        given = Path(run["input"]).read_text()
        for text in (f"sd:{item}: Make the thing work", "Make it, and the dashboard.", "Ruling: keep it small.",
                     f"design document: {design}"):
            self.assertIn(text, given)
        self.assertNotIn("opened as planning", given)
        self.assertIn("docs/dashboard/", (self.work / ".gitignore").read_text().splitlines())
        titles = "1. The done condition cannot fail; 2. Scope hides a second repository; 3. codex may refuse the model"
        self.assertEqual(self.notes(), [["task", "note", str(item), adversarial.NOTE.format(
            model="gpt-6-astra", effort="high", result=result, titles=titles), "--kind", "comment"]])
        self.assertEqual(self.snapshot(), before, "the review wrote to the database itself")

    def test_the_tool_config_names_the_model_and_effort(self):
        (self.config / "adversarial-gate").mkdir(parents=True)
        (self.config / "adversarial-gate" / ".env").write_text("ADVERSARIAL_MODEL=gpt-fixture\nADVERSARIAL_EFFORT=low\n")
        status, _, run = self.review("task", self.task())
        self.assertEqual(status, 202, run)
        self.settled(run["id"])
        self.assertEqual(self.codex_argv()[:5], ["exec", "-m", "gpt-fixture", "-c", "model_reasoning_effort=low"])

    def test_a_result_with_no_titled_hypothesis_says_so_in_the_note(self):
        self.result_fixture.write_text("Nothing survives.\n")
        item = self.task()
        status, _, run = self.review("task", str(item))
        self.assertEqual(status, 202, run)
        self.assertEqual(self.settled(run["id"])["state"], "done")
        self.assertIn("Top hypotheses: none titled in the result; read the file.", self.notes()[0][3])

    def test_a_git_ignored_results_folder_adds_no_gitignore_line(self):
        (self.work / ".gitignore").write_text("docs/\n")
        status, _, run = self.review("task", self.task())
        self.assertEqual(status, 202, run)
        self.settled(run["id"])
        self.assertEqual((self.work / ".gitignore").read_text(), "docs/\n")

    def test_a_task_without_a_repository_is_refused_and_runs_nothing(self):
        item = self.item("Personal errand", kind="task")
        status, _, answer = self.review("task", item)
        self.assertEqual(status, 400)
        self.assertIn("names no repository", answer["error"])
        self.assertFalse(self.argv.exists())
        # The refusal released the lock: the next review starts.
        status, _, run = self.review("task", self.task())
        self.assertEqual(status, 202, run)
        self.settled(run["id"])


class OneAtATime(AdversarialCase):
    def test_a_second_review_while_one_runs_is_refused_naming_the_first(self):
        self.mode.write_text("hold")
        status, _, first = self.review("task", self.task())
        self.assertEqual(status, 202, first)
        deadline = time.monotonic() + SETTLE
        while not self.argv.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.read(first["id"])[1]["state"], "running")
        status, _, refused = self.review("repo", self.key)
        self.assertEqual(status, 409, refused)
        self.assertIn(f"already running (run {first['id']})", refused["error"])
        self.assertIn("one runs at a time", refused["error"])
        self.release.write_text("")
        self.assertEqual(self.settled(first["id"])["state"], "done")
        status, _, after = self.review("repo", self.key)
        self.assertEqual(status, 202, after)
        self.settled(after["id"])


class FailedRuns(AdversarialCase):
    def test_a_codex_failure_reads_failed_with_its_own_words_and_notes_the_failure(self):
        self.mode.write_text("fail")
        item = self.task()
        status, _, run = self.review("task", item)
        self.assertEqual(status, 202, run)
        done = self.settled(run["id"])
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["reason"], "error: model gpt-6-astra is not served to this account (exit 3)")
        self.assertEqual(self.notes(), [["task", "note", str(item), adversarial.FAILED.format(
            model="gpt-6-astra", effort="high", reason=done["reason"], input=run["input"]), "--kind", "comment"]])

    def test_a_note_that_fails_is_reported_on_a_done_run(self):
        self.sd_fail.write_text("")
        status, _, run = self.review("task", self.task())
        self.assertEqual(status, 202, run)
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["note"]), ("done", "not written: sd task note failed: sd: item is locked"))

    def test_a_runner_that_died_reads_failed_naming_the_result_it_may_have_left(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        runs = self.state / "system" / "adversarial-gate" / "runs"
        runs.mkdir(parents=True)
        run_id = "repo-checkout-20261018T120000Z"
        (runs / f"{run_id}.json").write_text(json.dumps({
            "id": run_id, "kind": "repo", "target": self.key, "state": "running", "reason": None, "pid": gone.pid,
            "result": "/fixture/result.md", "item": None, "note": None, "started": "2026-10-18T12:00:00Z"}))
        status, document = self.read(run_id)
        self.assertEqual(status, 200)
        self.assertEqual(document["state"], "failed")
        self.assertEqual(document["reason"], f"the runner (pid {gone.pid}) stopped without recording an outcome; "
                                             f"look for /fixture/result.md")
        self.assertNotIn("pid", document)

    def test_a_queued_run_that_never_started_reads_failed_after_the_startup_window(self):
        runs = self.state / "system" / "adversarial-gate" / "runs"
        runs.mkdir(parents=True)
        run_id = "repo-checkout-20261018T120000Z"
        (runs / f"{run_id}.json").write_text(json.dumps({
            "id": run_id, "state": "queued", "pid": None, "result": "/fixture/result.md", "item": None,
            "started": "2026-10-18T12:00:00Z"}))
        early = datetime.datetime(2026, 10, 18, 12, 0, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(adversarial.status(run_id, now=early)[1]["state"], "queued")
        late = early + datetime.timedelta(seconds=adversarial.STARTUP)
        document = adversarial.status(run_id, now=late)[1]
        self.assertEqual((document["state"], document["reason"]),
                         ("failed", f"the runner never started within {adversarial.STARTUP} s"))

    def test_a_runner_that_died_before_its_note_reads_the_note_as_not_known(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        runs = self.state / "system" / "adversarial-gate" / "runs"
        runs.mkdir(parents=True)
        run_id = "task-7-20261018T120000Z"
        (runs / f"{run_id}.json").write_text(json.dumps({
            "id": run_id, "state": "done", "reason": None, "pid": gone.pid, "result": "/fixture/result.md", "item": 7,
            "note": None, "started": "2026-10-18T12:00:00Z"}))
        document = self.read(run_id)[1]
        self.assertEqual((document["state"], document["note"]),
                         ("done", "not known: the runner stopped before it recorded the note"))

    def test_a_runner_that_cannot_start_leaves_the_run_failed_and_the_lock_free(self):
        real = subprocess.Popen

        def refuse(argv, *args, **kwargs):
            if argv[0] == sys.executable:
                raise OSError("fork refused")
            return real(argv, *args, **kwargs)

        with patch.object(adversarial.subprocess, "Popen", side_effect=refuse):
            status, _, answer = self.review("task", self.task())
        self.assertEqual(status, 503, answer)
        self.assertIn("fork refused", answer["error"])
        [record] = (self.state / "system" / "adversarial-gate" / "runs").glob("*.json")
        document = self.read(record.stem)[1]
        self.assertEqual((document["state"], document["reason"]), ("failed", "the runner did not start: fork refused"))
        status, _, run = self.review("repo", self.key)
        self.assertEqual(status, 202, run)
        self.settled(run["id"])

    def test_an_unknown_run_is_a_404(self):
        self.assertEqual(self.read("repo-checkout-20261018T120000Z")[0], 404)
        self.assertEqual(self.read("..%2Fsecret")[0], 404)


class OtherKinds(AdversarialCase):
    def test_a_repo_review_reads_the_checkout_with_no_input_and_writes_no_note(self):
        status, _, run = self.review("repo", self.key)
        self.assertEqual(status, 202, run)
        self.assertRegex(run["id"], r"^repo-checkout-[0-9]{8}T[0-9]{6}Z$")
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["input"], done["item"], done["note"]), ("done", None, None, None))
        self.assertEqual(self.codex_argv()[7:9], ["-C", str(self.work)])
        self.assertIn("The document is a whole repository, `checkout`", self.stdin.read_text())
        self.assertEqual(self.notes(), [])

    def test_an_unregistered_repository_is_refused(self):
        status, _, answer = self.review("repo", str(self.root / "elsewhere"))
        self.assertEqual(status, 400)
        self.assertIn("is not a registered repository", answer["error"])

    def test_a_brief_is_reviewed_as_a_brief_and_noted_on_itself(self):
        brief = self.item("2026-10-18 - Morning", kind="report", source="brief", repo=self.key,
                          external_id="2026-10-18 - Morning", body={"text": "---\njob: morning\n---\n\nThree things."})
        status, _, run = self.review("brief", brief)
        self.assertEqual(status, 202, run)
        self.assertEqual(self.settled(run["id"])["note"], "written")
        self.assertIn("The document is a brief", self.stdin.read_text())
        self.assertIn("Three things.", Path(run["input"]).read_text())
        self.assertEqual(self.notes()[0][:3], ["task", "note", str(brief)])

    def test_a_task_is_not_reviewed_as_a_brief(self):
        status, _, answer = self.review("brief", self.task())
        self.assertEqual(status, 400)
        self.assertIn("is not a routine brief", answer["error"])

    def test_a_change_reads_its_diff_against_the_merge_base_with_its_body_and_notes_its_item(self):
        worktree = self.root / "wt-feature"
        git(self.work, "worktree", "add", "-q", "-b", "feature", str(worktree), "main")
        base = git(self.work, "rev-parse", "main")
        (worktree / "x.txt").write_text("x\n")
        git(worktree, "add", "x.txt")
        git(worktree, "commit", "-q", "-m", "add x")
        head = git(worktree, "rev-parse", "HEAD")
        body = self.root / "pr-body.md"
        body.write_text("## Summary\n\n- adds x\n")
        item = self.task()
        status, _, run = self.review("change", str(worktree), base="main", item=item, body=str(body))
        self.assertEqual(status, 202, run)
        self.assertRegex(run["id"], r"^change-wt-feature-[0-9]{8}T[0-9]{6}Z$")
        self.assertEqual(Path(run["result"]).parent, self.work / "docs" / "dashboard" / "adversarial")
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["note"]), ("done", "written"), done)
        self.assertEqual(self.codex_argv()[7:9], ["-C", str(worktree)])
        self.assertIn(f"git diff {base}...{head}", self.stdin.read_text())
        given = Path(run["input"]).read_text()
        for text in ("- branch: feature", f"- head: {head}", f"merge base {base}", f"- item: sd:{item}", "- adds x"):
            self.assertIn(text, given)
        self.assertEqual(self.notes()[0][:3], ["task", "note", str(item)])

    def test_a_change_with_no_commits_on_its_base_is_refused(self):
        status, _, answer = self.review("change", str(self.work), base="main")
        self.assertEqual(status, 400)
        self.assertIn("has no commits on top of main", answer["error"])

    def test_a_directory_outside_a_registered_repository_is_refused(self):
        stray = self.root / "stray"
        stray.mkdir()
        git(stray, "init", "-q", "-b", "main")
        status, _, answer = self.review("change", str(stray))
        self.assertEqual(status, 400)
        self.assertIn("is not a checkout of a registered repository", answer["error"])


class TheRequestShape(AdversarialCase):
    def test_bad_requests_are_refused_before_anything_runs(self):
        item = self.task()
        for payload, said in (({"kind": "essay", "target": item}, "kind is one of task, brief, repo, change"),
                              ({"kind": "task"}, "takes a kind and a target"),
                              ({"kind": "task", "target": item, "extra": 1}, "takes a kind and a target"),
                              ({"kind": "task", "target": item, "base": "main"}, "Only a change review"),
                              ({"kind": "task", "target": "sd:1"}, "The target is an item id."),
                              ({"kind": "change", "target": str(self.work), "base": "--output=x"}, "A base is a git ref")):
            with self.subTest(payload=payload):
                status, _, answer = self.post("/api/adversarial", payload)
                self.assertEqual(status, 400, answer)
                self.assertIn(said, answer["error"])
        self.assertFalse(self.argv.exists())

    def test_a_read_needs_a_dashboard_session(self):
        status, _, _ = self.request("/api/adversarial/repo-checkout-20261018T120000Z")
        self.assertEqual(status, 403)

    def test_a_missing_item_is_a_404(self):
        status, _, answer = self.review("task", 999999)
        self.assertEqual(status, 404, answer)
