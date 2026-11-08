"""The night triage job against a fake `sd`, `sd-db`, `claude` and `hostname` (sd:8143).

The fake `sd` keeps its items in a JSON file and logs every call; nothing
reaches the real model, launchd or the workflow database. The machine is
`node1`; `node2` is another machine of the fleet.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1]

FAKE_SD = r'''#!PYTHON
import json, os, sys
path = os.environ["FAKE_SD_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
state["log"].append(args)
items = state["items"]

def save():
    json.dump(state, open(path, "w"))

def show(key):
    item = items[key]
    return {"item": {"id": int(key), "status": item["status"]}, "notes": item["notes"],
            "revision": str(item["revision"])}

def flag(name):
    return args[args.index(name) + 1] if name in args else None

if args[:2] == ["reports", "list"]:
    print(json.dumps([{"id": int(k), "kind": "report", "status": v["status"], "title": v["title"],
                       "fields": json.dumps({"report": {"host": v["host"], "job": "job-" + k}})}
                      for k, v in items.items()]))
elif args[:2] == ["today", "--all"]:
    print("Attention (1)\n\nOperations, this machine (1)\n#1  job-1  a finding on node1\n\nDecisions (0)")
elif args[:2] == ["task", "show"]:
    print(json.dumps(show(args[2])))
elif args[:2] == ["task", "status"]:
    item = items[args[2]]
    if flag("--if-revision") not in (None, str(item["revision"])):
        save(); print("refused: the item changed", file=sys.stderr); sys.exit(1)
    item["status"] = args[3]; item["revision"] += 1
    save()
    if "--json" in args:
        print(json.dumps(show(args[2])))
elif args[:2] == ["task", "note"]:
    item = items[args[2]]
    item["notes"].append({"id": 900 + len(item["notes"]), "kind": "comment", "body": args[3], "resolved_at": None})
    item["revision"] += 1
elif args[:2] == ["reports", "acknowledge"]:
    item = items[args[2]]
    open_followups = [n for n in item["notes"] if n["kind"] == "followup" and not n["resolved_at"]]
    if open_followups and "--resolve-followups" not in args:
        save(); sys.exit(1)
    for note in open_followups:
        note["resolved_at"] = "now"
    item["status"] = "done"; item["revision"] += 1
else:
    save(); print("fake sd: unknown " + " ".join(args), file=sys.stderr); sys.exit(2)
save()
'''

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$CLAUDE_ARGS"
cat > "$CLAUDE_STDIN"
[ -f "$CLAUDE_REPLY" ] && cat "$CLAUDE_REPLY"
exit "${CLAUDE_RC:-0}"
"""


def report(host, status="planning", followups=()):
    return {"host": host, "status": status, "title": f"a job on {host}: needs attention", "revision": 1,
            "notes": [{"id": n, "kind": "followup", "body": f"followup {n}", "resolved_at": None}
                      for n in followups]}


class NightTriage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # A copy of the folder, so the job's state/ is the test's own.
        self.folder = self.tmp / "system/local-night-triage"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("state", "tests", "__pycache__"))
        shutil.copytree(FOLDER.parent / "lib", self.tmp / "system/lib")
        self.pack = self.tmp / "pack/bin"
        self.pack.mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.tool(self.pack / "sd", FAKE_SD.replace("PYTHON", sys.executable))
        self.tool(self.pack / "sd-db", "#!/bin/sh\necho 'fence: holder: node1'\n")
        self.tool(self.bin / "claude", FAKE_CLAUDE)
        self.tool(self.bin / "hostname", "#!/bin/sh\necho Node1\n")
        self.state = self.tmp / "sd.json"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", SD_PACK_ROOT=str(self.tmp / "pack"),
                        FAKE_SD_STATE=str(self.state), CLAUDE_ARGS=str(self.tmp / "args"),
                        CLAUDE_STDIN=str(self.tmp / "prompt"), CLAUDE_REPLY=str(self.tmp / "reply"),
                        TMPDIR=str(self.tmp))

    def tool(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def given(self, items, reply=None):
        self.state.write_text(json.dumps({"items": items, "log": []}))
        if reply is not None:
            (self.tmp / "reply").write_text("Done.\n" + json.dumps({"alerts": reply}))

    def run_job(self, rc=0):
        return subprocess.run(["sh", str(self.folder / "night-triage.sh"), "run"], capture_output=True, text=True,
                              env=dict(self.env, CLAUDE_RC=str(rc)), timeout=60)

    def items(self):
        return json.loads(self.state.read_text())["items"]

    def comments(self, key):
        return [n["body"] for n in self.items()[key]["notes"] if n["kind"] == "comment"]

    def test_a_fixed_alert_with_evidence_is_acknowledged(self):
        self.given({"1": report("node1", followups=[41])}, [
            {"id": 1, "outcome": "fixed", "summary": "a stale lock; cleared and reran",
             "evidence": ["cron-jobs.sh run job-1: exit 0"], "followups": {"41": "the rerun passed: exit 0"}}])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        item = self.items()["1"]
        self.assertEqual(item["status"], "done")
        self.assertEqual(len(self.comments("1")), 1)
        self.assertIn("Evidence: cron-jobs.sh run job-1: exit 0", self.comments("1")[0])
        self.assertIn("Followup #41: the rerun passed", self.comments("1")[0])
        self.assertIsNotNone(item["notes"][0]["resolved_at"])
        self.assertFalse((self.folder / "state/claims").exists())

    def test_a_fix_without_evidence_for_each_followup_stays_open(self):
        self.given({"1": report("node1", followups=[41, 42])}, [
            {"id": 1, "outcome": "fixed", "summary": "reran", "evidence": ["exit 0"], "followups": {"41": "ok"}}])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.items()["1"]["status"], "planning")
        self.assertIn("open followups without evidence: #42", self.comments("1")[0])
        self.assertNotIn(["reports", "acknowledge", "1", "--resolve-followups"],
                         json.loads(self.state.read_text())["log"])

    def test_a_needs_operator_alert_stays_open_with_one_note(self):
        self.given({"1": report("node1")}, [
            {"id": 1, "outcome": "operator", "summary": "a login expired",
             "action": "run `gh auth login` on node1", "why": "the login is interactive"}])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.items()["1"]["status"], "planning")
        self.assertEqual(self.comments("1"), [
            "night-triage on node1: needs the operator: run `gh auth login` on node1\n"
            "No tool can: the login is interactive"])

    def test_a_filed_bug_stays_open_and_cites_the_item(self):
        self.given({"1": report("node1")}, [
            {"id": 1, "outcome": "filed", "summary": "the parser drops a field", "item": "sd:77"}])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.items()["1"]["status"], "planning")
        self.assertIn("filed as sd:77", self.comments("1")[0])

    def test_another_machines_alert_is_untouched(self):
        self.given({"1": report("node1"), "2": report("node2")}, [
            {"id": 1, "outcome": "filed", "summary": "s", "item": "sd:77"},
            {"id": 2, "outcome": "fixed", "summary": "s", "evidence": ["e"]}])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.items()["2"], report("node2"))
        self.assertNotIn('"id": 2', (self.tmp / "prompt").read_text())

    def test_a_claimed_alert_is_skipped(self):
        self.given({"1": report("node1", status="in_progress")}, [
            {"id": 1, "outcome": "fixed", "summary": "s", "evidence": ["e"]}])
        result = self.run_job()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.items()["1"], report("node1", status="in_progress"))
        self.assertIn("sd:1 is claimed; skipped", result.stderr)

    def test_a_failed_model_run_leaves_the_alerts_as_they_were(self):
        self.given({"1": report("node1", followups=[41])}, [])
        result = self.run_job(rc=1)
        self.assertNotEqual(result.returncode, 0)
        item = self.items()["1"]
        self.assertEqual((item["status"], self.comments("1")), ("planning", []))
        self.assertFalse((self.folder / "state/claims").exists())

    def test_an_unreadable_reply_leaves_the_alerts_as_they_were(self):
        self.given({"1": report("node1")})
        (self.tmp / "reply").write_text("I could not finish.")
        self.assertNotEqual(self.run_job().returncode, 0)
        self.assertEqual((self.items()["1"]["status"], self.comments("1")), ("planning", []))

    def test_a_claim_a_crashed_run_left_is_released_by_the_next_run(self):
        alert = report("node1", status="in_progress")
        alert["revision"] = 5
        self.given({"1": alert}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:77"}])
        (self.folder / "state").mkdir()
        (self.folder / "state/claims").write_text("1 planning 5\n")
        self.assertEqual(self.run_job().returncode, 0)
        # Released, then claimed and settled by this run.
        self.assertEqual(self.items()["1"]["status"], "planning")
        self.assertIn("filed as sd:77", self.comments("1")[0])

    def test_a_left_claim_someone_touched_since_is_kept(self):
        alert = report("node1", status="in_progress")
        alert["revision"] = 6
        self.given({"1": alert}, [])
        (self.folder / "state").mkdir()
        (self.folder / "state/claims").write_text("1 planning 5\n")
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.items()["1"]["status"], "in_progress")

    def test_forbidden_tools_are_absent_from_the_allow_list(self):
        self.given({}, [])
        self.assertEqual(self.run_job().returncode, 0)
        args = (self.tmp / "args").read_text().splitlines()
        allowed = args[args.index("--allowedTools") + 1]
        for word in ("git push", "git merge", "gh ", "sd-ship", "sudo", "sd task status", "sd task note",
                     "sd reports acknowledge", "crontab", "install", "uninstall", "chmod", "chown",
                     "security", "osascript", "mail", "slack", "Write", "Edit", "rm -rf", "launchctl bootout",
                     "mcp__"):
            self.assertNotIn(word, allowed, word)
        for flag in ("--restricted", "--strict-mcp-config"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertIn("Write Edit", args[args.index("--disallowedTools") + 1])

    def test_the_prompt_names_this_machine_and_its_operations(self):
        self.given({"1": report("node1")}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:7"}])
        self.run_job()
        prompt = (self.tmp / "prompt").read_text()
        self.assertIn("Machine: node1 (hub)", prompt)
        self.assertIn("a finding on node1", prompt)
        self.assertNotIn("Decisions", prompt)

    def test_help_exits_zero_and_no_argument_exits_one(self):
        script = str(self.folder / "night-triage.sh")
        self.assertEqual(subprocess.run(["sh", script, "help"], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(["sh", script], capture_output=True).returncode, 1)


if __name__ == "__main__":
    unittest.main()
