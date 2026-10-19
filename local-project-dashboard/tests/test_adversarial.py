"""Adversarial review (sd:5064): a task, a brief, a repository or a change, reviewed by codex in the background.

Every test puts a `codex` of its own first on PATH: the double writes a fixed result to the `-o` path, or fails, or
holds until released, and journals its argv and stdin. The real `local-adversarial-gate/adversarial-gate.sh` runs
in front of it, with its Jev seam pointed at a path that does not exist. The runner files its outcome (a brief,
review batches and a note) in the fixture's temporary database, the one the server under test reads. So no test
reaches a model, the network, the judgment ledger or the live database. `test_the_double_is_what_the_run_finds`
asserts which `codex` the server's PATH resolves.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from sd_db import review, routine_briefs, workflow, writes

from sd_dashboard import adversarial

from test_workflow_actions import BrowserSession

#: The longest a run against the double may take before the case fails naming it.
SETTLE = 30
RESULT = """# Adversarial review

## 1. The done condition cannot fail

> "Make the thing work."

The item names no check. **What would have to be true:** no test exists. Confidence: **LIKELY**

- **Recommendation:** Add a test that fails before the fix
- Evidence: `README.md:1`
- Evidence: "Make the thing work."

## 2. Scope hides a second repository

> "and the dashboard"

Two repos move. **What would have to be true:** the page lives elsewhere. Confidence: **SPECULATIVE**
Subject: a line the review store would read as a field
Recommendation: Split the dashboard change into its own item
Repo: other

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
TITLES = "1. The done condition cannot fail; 2. Scope hides a second repository; 3. codex may refuse the model"


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
        (self.bin / "codex").write_text(CODEX)
        (self.bin / "codex").chmod(0o755)
        self.result_fixture = root / "result.md"
        self.result_fixture.write_text(RESULT)
        self.argv, self.stdin, self.mode = root / "codex.argv", root / "codex.stdin", root / "codex.mode"
        self.release = root / "codex.release"
        self.config = root / "config"
        environment = patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", "XDG_STATE_HOME": str(self.state),
            "SYSTEM_TOOLS_CONFIG": str(self.config), "ADVERSARIAL_GATE_JEV": str(root / "no-jev"),
            "CODEX_ARGV": str(self.argv), "CODEX_STDIN": str(self.stdin), "CODEX_MODE": str(self.mode),
            "CODEX_RELEASE": str(self.release), "CODEX_RESULT": str(self.result_fixture)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("ADVERSARIAL_MODEL", "ADVERSARIAL_EFFORT", "JEV_ADVERSARIAL_GATE"):
            os.environ.pop(name, None)
        git(self.work, "init", "-q", "-b", "main")
        (self.work / "README.md").write_text("# fixture\n")
        git(self.work, "add", "README.md")
        git(self.work, "commit", "-q", "-m", "first")
        self.key = self.repo(str(self.work))
        self.runs = self.state / "system" / "adversarial-gate" / "runs"
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
            if document["state"] in ("done", "failed") and (not adversarial._files(document) or document["filed"]):
                return document
            time.sleep(0.1)
        self.fail(f"run {run_id} did not settle in {SETTLE} s: {document}")

    def comments(self, item):
        return [note["body"] for note in workflow.item_state(self.connection, item)["notes"] if note["kind"] == "comment"]

    def brief(self, stem):
        row = writes.item_by_external(self.connection, routine_briefs.SOURCE, stem)
        return None if row is None else (row["id"], json.loads(row["body"])["text"], row["repo"])

    def batches(self):
        return [row["id"] for row in self.connection.execute(
            "SELECT id FROM item WHERE source = ? AND external_id LIKE 'review:adversarial:%' ORDER BY id",
            (review.REF_SOURCE,))]

    def codex_argv(self):
        return self.argv.read_text().splitlines()

    def task(self, title="Make the thing work", **columns):
        columns.setdefault("repo", self.key)
        return self.item(title, kind="task", **columns)

    def wait_for_codex(self):
        deadline = time.monotonic() + SETTLE
        while not self.argv.exists() and time.monotonic() < deadline:
            time.sleep(0.05)


class TheTaskReview(AdversarialCase):
    def test_the_double_is_what_the_run_finds(self):
        self.assertEqual(Path(shutil.which("codex")).parent, self.bin)

    def test_a_task_runs_codex_read_only_at_the_default_model_and_effort(self):
        design = self.work / "docs" / "work" / "plan" / "design.md"
        design.parent.mkdir(parents=True)
        design.write_text("# plan\n")
        git(self.work, "add", "docs")
        git(self.work, "commit", "-q", "-m", "plan")
        item = self.task(body={"text": "Make it, and the dashboard."}, path="docs/work/plan/design.md")
        self.note(item, "Ruling: keep it small.", kind="decision")
        status, _, run = self.review("task", item)
        self.assertEqual(status, 202, run)
        self.assertRegex(run["id"], rf"^task-{item}-[0-9]{{8}}T[0-9]{{6}}Z$")
        self.assertEqual(run["state"], "queued")
        result = self.runs / f"{run['id']}.md"
        self.assertEqual((run["result"], run["input"]), (str(result), str(self.runs / f"{run['id']}.input.md")))
        self.assertEqual((run["model"], run["effort"]), ("gpt-6-astra", "high"))
        self.assertNotIn("database", run)
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["reason"], done["filed"]), ("done", None, "written"), done)
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
        # Nothing lands in the reviewed checkout: no result file and no .gitignore line.
        self.assertEqual(git(self.work, "status", "--porcelain", "--ignored"), "")

    def test_the_setting_rows_name_the_model_and_effort(self):
        """sd:5109: the adversarial.model and adversarial.effort rows, not <config>/adversarial-gate/.env."""
        (self.config / "adversarial-gate").mkdir(parents=True)
        (self.config / "adversarial-gate" / ".env").write_text("ADVERSARIAL_MODEL=gpt-file\nADVERSARIAL_EFFORT=medium\n")
        writes.set_setting(self.connection, "adversarial.model", "gpt-fixture")
        writes.set_setting(self.connection, "adversarial.effort", "low")
        status, _, run = self.review("task", self.task())
        self.assertEqual(status, 202, run)
        self.settled(run["id"])
        self.assertEqual(self.codex_argv()[:5], ["exec", "-m", "gpt-fixture", "-c", "model_reasoning_effort=low"])

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


class TheOutcome(AdversarialCase):
    """Note #22943 on sd:5064: a brief listing the recommendations, and a review batch to pick them from."""

    def filed(self, item=None):
        item = item or self.task()
        status, _, run = self.review("task", item)
        self.assertEqual(status, 202, run)
        return item, self.settled(run["id"])

    def test_the_brief_lists_each_recommendation_and_names_its_batch(self):
        item, done = self.filed()
        self.assertEqual(done["filed"], "written", done)
        [batch] = self.batches()
        self.assertEqual(done["batches"], [batch])
        stem = done["brief"]
        self.assertRegex(stem, rf"^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}} - Adversarial review · task {item} · [0-9]{{6}}Z$")
        brief, text, repo = self.brief(stem)
        self.assertEqual((done["brief_item"], repo), (brief, self.key))
        self.assertTrue(text.startswith(f"---\nreview: adversarial\nrun: {done['id']}\ntarget: task {item}\n"
                                        f"model: gpt-6-astra\neffort: high\nbatches: sd:{batch}\n---\n"), text)
        for line in (f"- Review batches: sd:{batch} ({self.key}); approve a question there to file its task, "
                     f"reject it to drop it", "Does the central conclusion survive hostile reading? NO.",
                     "Recommendations: 4. These are hypotheses for a person to check, not findings.",
                     "### 1. Add a test that fails before the fix", "- Hypothesis: The done condition cannot fail",
                     "- Evidence: `README.md:1`", "### 2. Split the dashboard change into its own item",
                     "### 3. codex may refuse the model", "### 4. A fourth one", f"- Review batch: sd:{batch}",
                     f"- Repo: {self.key}"):
            self.assertIn(line, text.splitlines())
        self.assertEqual(text.count(f"- Review batch: sd:{batch}"), 4)
        self.assertEqual(routine_briefs.rows(self.connection)[0]["brief"]["series"],
                         f"Adversarial review · task {item} · {stem[-7:]}")

    def test_each_recommendation_is_one_approve_or_reject_question_whose_header_names_the_brief(self):
        item, done = self.filed()
        found = review.read(self.connection, done["batches"][0])
        self.assertEqual((found["header"]["queue"], found["header"]["source"]),
                         ("adversarial", f"local-adversarial-gate; brief {done['brief']}"))
        self.assertEqual(found["item"]["repo"], self.key)
        self.assertEqual(found["item"]["title"],
                         f"Adversarial review of task {item}: pick the recommendations to file as tasks")
        questions = found["questions"]
        self.assertEqual([q["options"] for q in questions], [["Approve", "Reject"]] * 4)
        self.assertEqual([q["text"].splitlines()[0] for q in questions],
                         ["Add a test that fails before the fix", "Split the dashboard change into its own item",
                          "codex may refuse the model", "A fourth one"])
        self.assertEqual([q["subject"] for q in questions],
                         [f"task {item}: The done condition cannot fail", f"task {item}: Scope hides a second repository",
                          f"task {item}: codex may refuse the model", f"task {item}: A fourth one"])
        self.assertEqual(questions[0]["evidence"], ["`README.md:1`", '"Make the thing work."'])
        self.assertIn("> Subject: a line the review store would read as a field", questions[1]["text"])
        self.assertTrue(questions[0]["text"].endswith(f"From the adversarial review in brief {done['brief']}."))
        # The verdict is the brief's summary, not the last hypothesis's reasoning.
        self.assertNotIn("survive hostile reading", questions[3]["text"])

    def test_the_item_s_note_names_the_brief_and_the_batch(self):
        item, done = self.filed()
        [batch] = done["batches"]
        self.assertEqual(self.comments(item), [adversarial.NOTE.format(
            model="gpt-6-astra", effort="high", stem=done["brief"], brief=done["brief_item"], count=4,
            batches=f"sd:{batch}", titles=TITLES)])

    def test_approving_a_question_files_its_task_in_the_recommendation_s_repository(self):
        _, done = self.filed()
        [batch] = done["batches"]
        first = review.read(self.connection, batch)["questions"][0]
        state = workflow.item_state(self.connection, batch)
        ruled = review.rule(self.connection, batch, first["note"], "Approve", expected_revision=state["revision"],
                            who="operator")
        task = workflow.item_state(self.connection, ruled["filed"]["item"])["item"]
        self.assertEqual((task["title"], task["repo"], task["kind"]), ("Add a test that fails before the fix",
                                                                       self.key, "task"))
        self.assertIn("Evidence: `README.md:1`", adversarial._text(task["body"]))
        self.assertIn(f"From the adversarial review in brief {done['brief']}.", adversarial._text(task["body"]))

    def test_a_recommendation_naming_another_registered_repository_is_asked_in_a_batch_of_its_own(self):
        other = self.root / "other"
        other.mkdir()
        other_key = self.repo(str(other))
        _, done = self.filed()
        home, away = done["batches"]
        self.assertEqual([q["text"].splitlines()[0] for q in review.read(self.connection, away)["questions"]],
                         ["Split the dashboard change into its own item"])
        self.assertEqual(len(review.read(self.connection, home)["questions"]), 3)
        _, text, _ = self.brief(done["brief"])
        self.assertIn(f"- Review batches: sd:{home} ({self.key}), sd:{away} ({other_key}); approve a question there "
                      f"to file its task, reject it to drop it", text)
        self.assertIn(f"- Repo: {other_key}", text)
        question = review.read(self.connection, away)["questions"][0]
        ruled = review.rule(self.connection, away, question["note"], "Approve",
                            expected_revision=workflow.item_state(self.connection, away)["revision"], who="operator")
        task = workflow.item_state(self.connection, ruled["filed"]["item"])["item"]
        self.assertEqual((task["title"], task["repo"]), ("Split the dashboard change into its own item", other_key))

    def test_a_result_with_no_titled_hypothesis_files_a_brief_and_no_batch(self):
        self.result_fixture.write_text("Nothing survives.\n")
        item, done = self.filed()
        self.assertEqual((done["state"], done["filed"], done["batches"]), ("done", "written", []), done)
        self.assertEqual(self.batches(), [])
        _, text, _ = self.brief(done["brief"])
        self.assertIn("- Review batches: none; approve a question there to file its task, reject it to drop it", text)
        self.assertIn("Recommendations: 0.", text)
        [note] = self.comments(item)
        self.assertIn("recommendations: 0, to pick in review batch none. Top hypotheses: none titled in the result; "
                      "read the brief.", note)

    def test_a_recommendation_the_queue_rejected_is_not_asked_again(self):
        item = self.task()
        _, done = self.filed(item)
        [batch] = done["batches"]
        first = review.read(self.connection, batch)["questions"][0]
        review.rule(self.connection, batch, first["note"], "Reject",
                    expected_revision=workflow.item_state(self.connection, batch)["revision"], who="operator")
        time.sleep(1.1)  # The next run's id carries the next second.
        _, again = self.filed(item)
        [second] = again["batches"]
        self.assertEqual(len(review.read(self.connection, second)["questions"]), 3)
        _, text, _ = self.brief(again["brief"])
        self.assertIn("- Not asked: ", text)

    def test_parsing_takes_the_tagged_lines_and_quotes_a_reserved_one(self):
        found, verdict = adversarial.recommendations(RESULT)
        self.assertEqual(verdict, "Does the central conclusion survive hostile reading? NO.")
        self.assertEqual([(one["title"], one["repo"]) for one in found[:2]],
                         [("Add a test that fails before the fix", None),
                          ("Split the dashboard change into its own item", "other")])
        self.assertNotIn("Recommendation:", found[0]["why"])
        self.assertIn("> Subject:", found[1]["why"])
        many = "".join(f"## {n}. Hypothesis {n}\n\n{'x' * 5000}\nEvidence: a\nConfidence: **LIKELY**\n\n"
                       for n in range(1, 41))
        found, _ = adversarial.recommendations(many)
        self.assertEqual(len(found), adversarial.RECOMMENDATIONS)
        self.assertLessEqual(max(len(one["why"].encode()) for one in found), adversarial.WHY_BYTES + 4)


class OneAtATime(AdversarialCase):
    def test_a_second_review_while_one_runs_is_refused_naming_the_first(self):
        self.mode.write_text("hold")
        status, _, first = self.review("task", self.task())
        self.assertEqual(status, 202, first)
        self.wait_for_codex()
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


    def test_the_lock_is_free_once_the_run_reads_settled(self):
        """A page that starts the next review as soon as the last reads settled gets a 202, not a 409: the runner
        lets go of the lock before its last state write (a test that posted at once failed under load)."""
        import fcntl

        import sd_db

        self.runs.mkdir(parents=True)
        run_id = "repo-checkout-20261018T120000Z"
        record = self.runs / f"{run_id}.json"
        record.write_text(json.dumps({
            "id": run_id, "kind": "repo", "target": self.key, "label": "checkout", "state": "queued", "reason": None,
            "repo": str(self.work), "checkout": str(self.work), "result": str(self.runs / f"{run_id}.md"),
            "input": None, "item": None, "sets": {"REPO_NAME": "checkout"}, "model": "gpt-6-astra", "effort": "high",
            "timeout": 60, "started": "2026-10-18T12:00:00Z", "finished": None, "filed": None, "brief": None,
            "brief_item": None, "batches": None, "pid": None, "database": str(self.path),
            "library": str(Path(sd_db.__file__).resolve().parent.parent)}))
        held = open(self.runs.parent / "lock", "a+")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock = os.dup(held.fileno())
        held.close()
        self.assertEqual(adversarial.run(record, lock), 0)
        self.assertEqual(json.loads(record.read_text())["filed"], "written")
        with open(self.runs.parent / "lock", "a+") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


class FailedRuns(AdversarialCase):
    def test_a_failed_settings_read_starts_no_run_and_releases_the_lock(self):
        """sd:5109: the model and effort are unknown when their rows do not read: no run file, codex never runs."""
        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(writes, "read_settings", refuse):
            status, _, answer = self.review("task", self.task())
        self.assertEqual(status, 503, answer)
        self.assertIn("did not read from the workflow database", answer["error"])
        self.assertEqual(sorted(self.runs.glob("*")) if self.runs.exists() else [], [])
        self.assertFalse(self.argv.exists())
        status, _, run = self.review("task", self.task())
        self.assertEqual(status, 202, run)
        self.settled(run["id"])

    def test_a_codex_failure_reads_failed_with_its_own_words_and_notes_the_failure(self):
        self.mode.write_text("fail")
        item = self.task()
        status, _, run = self.review("task", item)
        self.assertEqual(status, 202, run)
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["filed"], done["brief"]), ("failed", "written", None))
        self.assertEqual(done["reason"], "error: model gpt-6-astra is not served to this account (exit 3)")
        self.assertEqual(self.comments(item), [adversarial.FAILED.format(
            model="gpt-6-astra", effort="high", reason=done["reason"], input=run["input"])])
        self.assertEqual(self.batches(), [])

    def test_a_filing_that_fails_writes_nothing_and_says_why(self):
        # The brief's stem is taken by another text, so the brief fails after its batch was filed: the one
        # transaction takes the batch and the note back with it.
        self.mode.write_text("hold")
        item = self.task()
        status, _, run = self.review("task", item)
        self.assertEqual(status, 202, run)
        stem = adversarial._stem({**run, "label": str(item)})
        routine_briefs.add(self.connection, stem, "---\nseries: x\n---\n\nAnother text.\n", repo=self.key, who="test")
        self.release.write_text("")
        done = self.settled(run["id"])
        self.assertEqual(done["state"], "done")
        self.assertTrue(done["filed"].startswith("not written: BriefConflict: "), done["filed"])
        self.assertEqual((done["brief"], done["batches"]), (None, None))
        self.assertEqual((self.batches(), self.comments(item)), ([], []))

    def test_a_runner_that_died_reads_failed_naming_the_result_it_may_have_left(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.runs.mkdir(parents=True)
        run_id = "repo-checkout-20261018T120000Z"
        (self.runs / f"{run_id}.json").write_text(json.dumps({
            "id": run_id, "kind": "repo", "target": self.key, "state": "running", "reason": None, "pid": gone.pid,
            "result": "/fixture/result.md", "item": None, "filed": None, "started": "2026-10-18T12:00:00Z"}))
        status, document = self.read(run_id)
        self.assertEqual(status, 200)
        self.assertEqual(document["state"], "failed")
        self.assertEqual(document["reason"], f"the runner (pid {gone.pid}) stopped without recording an outcome; "
                                             f"look for /fixture/result.md")
        self.assertNotIn("pid", document)

    def test_a_queued_run_that_never_started_reads_failed_after_the_startup_window(self):
        self.runs.mkdir(parents=True)
        run_id = "repo-checkout-20261018T120000Z"
        (self.runs / f"{run_id}.json").write_text(json.dumps({
            "id": run_id, "state": "queued", "pid": None, "result": "/fixture/result.md", "item": None,
            "started": "2026-10-18T12:00:00Z"}))
        early = datetime.datetime(2026, 10, 18, 12, 0, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(adversarial.status(run_id, now=early)[1]["state"], "queued")
        late = early + datetime.timedelta(seconds=adversarial.STARTUP)
        document = adversarial.status(run_id, now=late)[1]
        self.assertEqual((document["state"], document["reason"]),
                         ("failed", f"the runner never started within {adversarial.STARTUP} s"))

    def test_a_runner_that_died_before_its_filing_reads_the_filing_as_not_known(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.runs.mkdir(parents=True)
        run_id = "repo-checkout-20261018T120000Z"
        (self.runs / f"{run_id}.json").write_text(json.dumps({
            "id": run_id, "kind": "repo", "label": "checkout", "state": "done", "reason": None, "pid": gone.pid,
            "result": "/fixture/result.md", "item": None, "filed": None, "started": "2026-10-18T12:00:00Z"}))
        document = self.read(run_id)[1]
        self.assertEqual((document["state"], document["filed"]),
                         ("done", "not known: the runner stopped before it recorded the filing; look for brief "
                                  "2026-10-18 - Adversarial review · repo checkout · 120000Z on the Briefs page"))

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
        [record] = self.runs.glob("*.json")
        document = self.read(record.stem)[1]
        self.assertEqual((document["state"], document["reason"]), ("failed", "the runner did not start: fork refused"))
        status, _, run = self.review("repo", self.key)
        self.assertEqual(status, 202, run)
        self.settled(run["id"])

    def test_an_unknown_run_is_a_404(self):
        self.assertEqual(self.read("repo-checkout-20261018T120000Z")[0], 404)
        self.assertEqual(self.read("..%2Fsecret")[0], 404)


class OtherKinds(AdversarialCase):
    def test_a_repo_review_reads_the_checkout_with_no_input_and_files_a_brief_but_no_note(self):
        status, _, run = self.review("repo", self.key)
        self.assertEqual(status, 202, run)
        self.assertRegex(run["id"], r"^repo-checkout-[0-9]{8}T[0-9]{6}Z$")
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["input"], done["item"], done["filed"]), ("done", None, None, "written"))
        self.assertEqual(self.codex_argv()[7:9], ["-C", str(self.work)])
        self.assertIn("The document is a whole repository, `checkout`", self.stdin.read_text())
        self.assertIsNotNone(self.brief(done["brief"]))
        self.assertEqual(len(done["batches"]), 1)

    def test_a_checkout_whose_path_holds_a_quote_is_reviewed(self):
        # The gate passes its words without `eval` now (sd:5064), so the dashboard no longer refuses such a path.
        quoted = self.root / "it's-repo"
        quoted.mkdir()
        git(quoted, "init", "-q", "-b", "main")
        key = self.repo(str(quoted))
        status, _, run = self.review("repo", key)
        self.assertEqual(status, 202, run)
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["filed"]), ("done", "written"), done)
        self.assertEqual(self.codex_argv()[7:9], ["-C", str(quoted)])

    def test_an_unregistered_repository_is_refused(self):
        status, _, answer = self.review("repo", str(self.root / "elsewhere"))
        self.assertEqual(status, 400)
        self.assertIn("is not a registered repository", answer["error"])

    def test_a_brief_is_reviewed_as_a_brief_and_noted_on_itself(self):
        brief = self.item("2026-10-18 - Morning", kind="report", source="brief", repo=self.key,
                          external_id="2026-10-18 - Morning", body={"text": "---\njob: morning\n---\n\nThree things."})
        status, _, run = self.review("brief", brief)
        self.assertEqual(status, 202, run)
        done = self.settled(run["id"])
        self.assertEqual(done["filed"], "written", done)
        self.assertIn("The document is a brief", self.stdin.read_text())
        self.assertIn("Three things.", Path(run["input"]).read_text())
        [note] = self.comments(brief)
        self.assertIn(f"brief {done['brief']} (sd:{done['brief_item']})", note)

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
        done = self.settled(run["id"])
        self.assertEqual((done["state"], done["filed"]), ("done", "written"), done)
        self.assertEqual(self.codex_argv()[7:9], ["-C", str(worktree)])
        self.assertIn(f"git diff {base}...{head}", self.stdin.read_text())
        given = Path(run["input"]).read_text()
        for text in ("- branch: feature", f"- head: {head}", f"merge base {base}", f"- item: sd:{item}", "- adds x"):
            self.assertIn(text, given)
        [note] = self.comments(item)
        self.assertIn(f"brief {done['brief']}", note)
        # The batch and the brief belong to the main repository, not the worktree.
        self.assertEqual(review.read(self.connection, done["batches"][0])["item"]["repo"], self.key)

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
