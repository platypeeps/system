"""The nightly review job against a scratch database and a stub `claude` (sd:3638).

Each test builds a HOME of its own, initialises the workflow database there
with the pack's `bin/sd-db` (SD_PACK_ROOT, the pinned pack in the gate), sets
`sd.bulk_storage_root` to a scratch folder, and puts a recording `claude`
first on PATH. Nothing reaches the real model, launchd or the live database.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1]
SCRIPT = FOLDER / "nightly-review.sh"
PACK = Path(os.environ.get("SD_PACK_ROOT") or Path.home() / "repos/platypeeps/sd-ai-command-pack")
# The pack's library first: an older installed copy has no `review` module.
sys.path.insert(0, str(PACK / "lib"))
from sd_db import connect, review, workflow  # noqa: E402

# A `claude` that records its arguments and prompt, then prints the reply
# file and exits with CLAUDE_RC.
FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$CLAUDE_ARGS"
for arg in "$@"; do last=$arg; done
echo x >> "$CLAUDE_CALLS"
[ -f "$CLAUDE_REPLY" ] && cat "$CLAUDE_REPLY"
exit "${CLAUDE_RC:-0}"
"""

SUGGESTIONS = {"questions": [
    {"subject": "lane:repeat-blocker", "text": "Add a lint for the repeat blocker\nSaves one review round a week.",
     "evidence": ["note #12", "log: example.test/lane.log"]},
    {"subject": "ops:stale-service", "text": "Restart the stale dashboard after a hub refresh", "evidence": []},
]}


class NightlyReview(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nightly-review-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bulk = self.tmp / "bulk"
        self.bulk.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.reply = self.tmp / "reply"
        self.calls = self.tmp / "claude-calls"
        self.args = self.tmp / "claude-args"
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "SYSTEM_TOOLS_CONFIG", "CLAUDE_BIN")}
        self.env.update(HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}", SD_PACK_ROOT=str(PACK),
                        CLAUDE_REPLY=str(self.reply), CLAUDE_CALLS=str(self.calls), CLAUDE_ARGS=str(self.args))
        self.pack(PACK, "sd-db", "init")
        self.pack(PACK, "sd", "config", "set", "sd.bulk_storage_root", str(self.bulk))
        self.today = date.today().isoformat()
        self.batch = self.bulk / "system" / "nightly-review" / f"{self.today}.json"

    def pack(self, pack, program, *args):
        done = subprocess.run([str(pack / "bin" / program), *args], env=self.env, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def run_job(self, *args, reply=SUGGESTIONS, rc=0):
        if reply is None:
            self.reply.unlink(missing_ok=True)
        else:
            self.reply.write_text(reply if isinstance(reply, str) else json.dumps(reply))
        env = dict(self.env, CLAUDE_RC=str(rc))
        return subprocess.run(["sh", str(SCRIPT), *(args or ("run",))], env=env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)

    def connection(self):
        connection = connect(self.home / ".local/share/sd/sd.db")
        self.addCleanup(connection.close)
        return connection

    def batches(self):
        rows = self.connection().execute(
            "SELECT id FROM item WHERE source = 'task-ref' AND external_id LIKE 'review:nightly:%' ORDER BY id")
        return [review.read(self.connection(), row["id"]) for row in rows]

    def model_runs(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def file_earlier(self, name, subjects):
        """File a nightly batch from an earlier day, as the job would have."""
        path = self.tmp / f"{name}.json"
        path.write_text(json.dumps({
            "schema": 1, "queue": "nightly", "batch": name, "title": f"Nightly review {name}",
            "source": "system@0000000", "mode": "open", "limit": 5,
            "questions": [{"subject": subject, "text": f"Do {subject}", "options": ["Approve", "Reject"]}
                          for subject in subjects]}))
        self.pack(PACK, "sd-db", "review", "file", str(path))
        return next(found for found in self.batches() if found["header"]["batch"] == name)

    def rule(self, found, subject, option):
        connection = self.connection()
        item = found["item"]["id"]
        note = next(q["note"] for q in found["questions"] if q["subject"] == subject)
        review.rule(connection, item, note, option, who="test",
                    expected_revision=workflow.item_state(connection, item)["revision"])

    def written(self):
        return json.loads(self.batch.read_text())

    # ------------------------------------------------------------ one night

    def test_a_run_files_one_open_batch_of_the_models_suggestions(self):
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        written = self.written()
        self.assertEqual((written["queue"], written["batch"], written["mode"], written["limit"]),
                         ("nightly", self.today, "open", 5))
        self.assertEqual([q["options"] for q in written["questions"]], [["Approve", "Reject"]] * 2)
        # Every question is the model's own suggestion, so the page recommends Approve (sd:5634).
        self.assertEqual([q["evidence"][-1] for q in written["questions"]], ["Proposed label: Approve"] * 2)
        found = self.batches()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["ref"], f"review:nightly:{self.today}")
        self.assertEqual([q["subject"] for q in found[0]["questions"]], ["lane:repeat-blocker", "ops:stale-service"])
        self.assertEqual(found[0]["item"]["title"], f"Nightly review {self.today}")
        self.assertTrue(found[0]["header"]["source"].startswith("system@"), found[0]["header"])

    def test_a_second_run_the_same_day_runs_no_model_and_files_nothing_new(self):
        first = self.run_job()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.run_job(reply={"questions": [{"subject": "other:idea", "text": "Something else"}]})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.model_runs(), 1)
        self.assertEqual(len(self.batches()), 1)
        self.assertIn("filed already", second.stdout)

    def test_the_model_gets_sonnet_no_write_tool_and_no_stdin(self):
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.args.read_text().splitlines()
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        tools = args[args.index("--allowedTools") + 1]
        for writer in ("Write", "Edit", "sd-db", "sd task add", "sd task note", "sd task status", "sd note",
                       "git -C", "Bash(sd:", "Bash(git", "Bash(sh", "Bash(python"):
            # REGRESSION sd:3638 review: git log and git show write a file through --output=<path>.
            self.assertNotIn(writer, tools)
        self.assertIn("Write", args[args.index("--disallowedTools") + 1])

    def test_the_model_inherits_no_permission_or_mcp_server(self):
        # REGRESSION sd:3638 review: --allowedTools only adds grants; inherited Bash or MCP grants still applied.
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.args.read_text().splitlines()
        self.assertIn("--restricted", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob,Bash")

    def test_a_run_with_no_suggestions_files_nothing_and_passes(self):
        result = self.run_job(reply={"questions": []})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.written()["questions"], [])
        self.assertEqual(self.batches(), [])
        self.assertIn("no suggestions", result.stdout)

    def test_more_than_five_suggestions_file_the_first_five(self):
        many = {"questions": [{"subject": f"idea:{n}", "text": f"Idea {n}"} for n in range(1, 8)]}
        result = self.run_job(reply=many)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        found = self.batches()
        self.assertEqual([q["subject"] for q in found[0]["questions"]], [f"idea:{n}" for n in range(1, 6)])
        self.assertIn("idea:6: past the limit of 5", result.stdout)

    def test_long_evidence_keeps_four_lines_so_the_label_line_fits(self):
        # sd:6602: five lines plus the label made six, and sd-db refused the whole batch.
        good = SUGGESTIONS["questions"][0]
        for count in (5, 9):
            with self.subTest(count=count):
                for path in (self.batch, self.batch.with_name(self.batch.stem + ".reply")):
                    path.unlink(missing_ok=True)
                lines = [f"source {n}" for n in range(1, count + 1)]
                result = self.run_job(reply={"questions": [{**good, "evidence": lines}]})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                filed = json.loads(self.batch.read_text(encoding="utf-8"))["questions"]
                self.assertEqual(filed[0]["evidence"], [*lines[:4], "Proposed label: Approve"])

    # ------------------------------------------------------------ failures are unknown, not "no suggestions"

    def test_a_failed_model_run_writes_no_file_files_nothing_and_fails(self):
        result = self.run_job(rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.batch.exists())
        self.assertEqual(self.batches(), [])

    def test_a_reply_that_is_not_a_batch_writes_no_file_and_fails(self):
        for reply in ("I found nothing worth suggesting.", '{"questions": "none"}',
                      {"questions": [{"subject": "a:b"}]},
                      {"questions": [{"subject": "a:b", "text": "t", "options": ["Yes", "No"]}]}):
            with self.subTest(reply=reply):
                result = self.run_job(reply=reply)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(self.batch.exists())
                self.assertFalse(self.batch.with_name(self.batch.name + ".part").exists())
                self.assertEqual(self.batches(), [])

    def test_one_malformed_suggestion_drops_itself_and_the_rest_file(self):
        # sd:5732: an extra key on suggestion 2 refused the whole night's batch.
        good = SUGGESTIONS["questions"][0]
        for bad in ({**good, "rank": 2}, {**good, "evidence": "one string"}):
            with self.subTest(bad=bad):
                for path in (self.batch, self.batch.with_name(self.batch.stem + ".reply")):
                    path.unlink(missing_ok=True)
                result = self.run_job(reply={"questions": [good, bad]})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("suggestion 2 dropped", result.stderr)
                filed = json.loads(self.batch.read_text(encoding="utf-8"))["questions"]
                self.assertEqual([q["subject"] for q in filed], [good["subject"]])
                self.assertTrue(self.batch.with_name(self.batch.stem + ".reply").exists())

    def test_a_reply_wrapped_in_prose_or_a_fence_still_files(self):
        fenced = "Here are tonight's suggestions:\n```json\n" + json.dumps(SUGGESTIONS) + "\n```\n"
        result = self.run_job(reply=fenced)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.batches()[0]["questions"]), 2)

    def test_no_bulk_storage_root_fails_before_the_model(self):
        self.pack(PACK, "sd", "config", "unset", "sd.bulk_storage_root")
        result = self.run_job()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("sd.bulk_storage_root", result.stderr)
        self.assertEqual(self.model_runs(), 0)

    def test_an_unmounted_bulk_storage_root_fails_before_the_model(self):
        self.bulk.rmdir()
        result = self.run_job()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not a folder", result.stderr)
        self.assertEqual(self.model_runs(), 0)

    def test_a_pack_without_review_fails_before_the_model(self):
        """Older state: a pack rolled back before `sd-db review` exits 1 at apply."""
        old = self.tmp / "old-pack" / "bin"
        old.mkdir(parents=True)
        (old / "sd").symlink_to(PACK / "bin" / "sd")
        (old / "sd-db").write_text('#!/bin/sh\necho "sd-db: unknown verb $1" >&2\nexit 2\n')
        (old / "sd-db").chmod(0o755)
        self.env["SD_PACK_ROOT"] = str(old.parent)
        result = self.run_job()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.model_runs(), 0)
        self.assertFalse(self.batch.exists())

    def test_a_failed_read_of_earlier_rejections_fails_before_the_model(self):
        broken = self.tmp / "broken-pack" / "bin"
        broken.mkdir(parents=True)
        (broken / "sd-db").symlink_to(PACK / "bin" / "sd-db")
        (broken / "sd").write_text(f'#!/bin/sh\n[ "$1" = store ] && exit 1\nexec "{PACK}/bin/sd" "$@"\n')
        (broken / "sd").chmod(0o755)
        self.env["SD_PACK_ROOT"] = str(broken.parent)
        result = self.run_job()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.model_runs(), 0)
        self.assertEqual(self.batches(), [])

    def test_a_batch_the_store_refuses_fails_and_files_nothing(self):
        long_subject = {"questions": [{"subject": "x" * 201, "text": "Too long a subject"}]}
        result = self.run_job(reply=long_subject)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.batches(), [])

    # ------------------------------------------------------------ the lead's guard: open batches only

    def test_the_job_files_no_blind_or_sealed_batch(self):
        """No blind or sealed batch until the Review page lands (lead, sd:3635 #16345)."""
        self.batch.parent.mkdir(parents=True)
        for mode, extra in (("blind", {}), ("sealed", {"seal": {"sha256": "0" * 64, "name": "key.csv"}})):
            with self.subTest(mode=mode):
                self.batch.write_text(json.dumps({
                    "schema": 1, "queue": "nightly", "batch": self.today, "title": "Nightly review",
                    "source": "system@0000000", "mode": mode, "limit": 5, **extra,
                    "questions": [{"subject": "a:b", "text": "t", "options": ["Approve", "Reject"]}]}))
                result = self.run_job()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("open batches only", result.stderr)
                self.assertEqual(self.batches(), [])
                self.assertEqual(self.model_runs(), 0)

    def test_a_reply_that_asks_for_a_mode_is_refused(self):
        result = self.run_job(reply={"mode": "blind", **SUGGESTIONS})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.batch.exists())
        self.assertEqual(self.batches(), [])

    def test_a_batch_file_for_another_queue_or_limit_is_refused(self):
        self.batch.parent.mkdir(parents=True)
        base = {"schema": 1, "queue": "nightly", "batch": self.today, "title": "Nightly review",
                "source": "system@0000000", "mode": "open", "limit": 5,
                "questions": [{"subject": "a:b", "text": "t", "options": ["Approve", "Reject"]}]}
        for change in ({"queue": "labels"}, {"limit": 500}, {"batch": "other"},
                       {"questions": [{"subject": "a:b", "text": "t", "options": ["Approve", "Reject", "Defer"]}]}):
            with self.subTest(change=change):
                self.batch.write_text(json.dumps({**base, **change}))
                result = self.run_job()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self.batches(), [])

    # ------------------------------------------------------------ steps: a stop between each pair

    def test_a_stop_after_the_reply_before_the_rename_runs_the_model_again(self):
        """A `.part` left by a stopped run is not a batch: the rerun asks again and files once."""
        self.batch.parent.mkdir(parents=True)
        self.batch.with_name(self.batch.name + ".part").write_text('{"half": ')
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.model_runs(), 1)
        self.assertEqual(len(self.batches()), 1)

    def test_a_stop_after_the_rename_before_filing_files_without_the_model(self):
        """A batch file in place, as a run stopped before `sd-db review file` left it."""
        self.batch.parent.mkdir(parents=True)
        self.batch.write_text(json.dumps({
            "schema": 1, "queue": "nightly", "batch": self.today, "title": f"Nightly review {self.today}",
            "source": "system@0000000", "mode": "open", "limit": 5,
            "questions": [{"subject": "a:b", "text": "t", "evidence": [], "options": ["Approve", "Reject"]}]}))
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.model_runs(), 0)
        self.assertEqual([q["subject"] for q in self.batches()[0]["questions"]], ["a:b"])

    # ------------------------------------------------------------ apply and the rejected subjects

    def test_apply_turns_an_earlier_approval_into_a_task_first(self):
        earlier = self.file_earlier("2026-01-02", ["ops:disk-alert"])
        self.rule(earlier, "ops:disk-alert", "Approve")
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        note = earlier["questions"][0]["note"]
        row = self.connection().execute("SELECT id FROM item WHERE external_id = ?",
                                        (f"review-approved:{note}",)).fetchone()
        self.assertIsNotNone(row, result.stdout)
        self.assertLess(result.stdout.index("approved note"), result.stdout.index(f"review:nightly:{self.today}, "))

    def test_the_model_hears_each_subject_rejected_in_90_days_and_a_repeat_is_dropped(self):
        earlier = self.file_earlier("2026-01-02", ["lane:repeat-blocker", "ops:kept-open"])
        self.rule(earlier, "lane:repeat-blocker", "Reject")
        self.rule(earlier, "ops:kept-open", "Approve")
        # The batch is closed by apply; its rejection still counts.
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompt = self.args.read_text()
        self.assertIn("- lane:repeat-blocker", prompt)
        self.assertNotIn("ops:kept-open", prompt)
        today = next(found for found in self.batches() if found["header"]["batch"] == self.today)
        self.assertEqual([q["subject"] for q in today["questions"]], ["ops:stale-service"])
        self.assertIn("dropped lane:repeat-blocker", result.stdout)

    # ------------------------------------------------------------ the entrypoint

    def test_help_and_no_argument(self):
        for form in ("help", "-h", "--help"):
            with self.subTest(form=form):
                result = self.run_job(form)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage", result.stdout)
        result = subprocess.run(["sh", str(SCRIPT)], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
