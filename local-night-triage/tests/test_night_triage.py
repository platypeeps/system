"""The night triage job against a fake `sd`, `sd-db`, `claude` and `hostname` (sd:8143).

The fake `sd` keeps its items in a JSON file and logs every call; nothing
reaches the real model, launchd or the workflow database. The machine is
`node1`; `node2` is another machine of the fleet.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1]


def day(offset):
    """A date `offset` days from today, local time, as the job reads it."""
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()

FAKE_SD = r'''#!PYTHON
import json, os, sys
path = os.environ["FAKE_SD_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
state["log"].append(args)
items = state["items"]
# FAKE_SD_DOWN names a verb ("task note") that fails as an outage does.
if " ".join(args).startswith(os.environ.get("FAKE_SD_DOWN") or "\0"):
    json.dump(state, open(path, "w")); print("database unavailable", file=sys.stderr); sys.exit(1)

def save():
    json.dump(state, open(path, "w"))

def show(key):
    item = items[key]
    return {"item": {"id": int(key), "status": item["status"], "parked_at": item.get("parked_at")},
            "notes": item["notes"],
            "revision": str(item["revision"])}

def flag(name):
    return args[args.index(name) + 1] if name in args else None

# Each verb takes only the flags the pinned pack's parser does: an unknown
# one exits 2, as argparse does (sd:8143 review: `sd today --all`).
FLAGS = {"reports list": {"--json"}, "today": {"--json", "--only"}, "task show": {"--json"},
         "task status": {"--json", "--if-revision", "--reason"}, "task note": {"--json", "--if-revision"},
         "reports acknowledge": {"--json", "--if-revision", "--resolve-followups"},
         "task edit": {"--json", "--if-revision", "--park", "--unpark"}}
verb = "today" if args[:1] == ["today"] else " ".join(args[:2])
if verb not in FLAGS or any(a.startswith("--") and a not in FLAGS[verb] for a in args):
    save(); print("fake sd: unrecognized arguments: " + " ".join(args), file=sys.stderr); sys.exit(2)

if args[:2] == ["reports", "list"]:
    print(json.dumps([{"id": int(k), "kind": "report", "status": v["status"], "title": v["title"],
                       "parked_at": v.get("parked_at"),
                       "fields": json.dumps({"report": {"host": v["host"], "job": "job-" + k},
                                             **({"repeats": {"count": v["runs"]}} if "runs" in v else {})})}
                      for k, v in items.items()]))
elif args[:1] == ["today"]:
    print("Attention (1)\n\nOperations, this machine (1)\n#1  job-1  a finding on node1\n\nDecisions (0)")
elif args[:2] == ["task", "show"]:
    print(json.dumps(show(args[2])))
elif args[:2] == ["task", "status"]:
    # As the pack: a report takes no status through task controls.
    save(); print(f"sd: report item {args[2]} cannot move to {args[3]} through task controls", file=sys.stderr)
    sys.exit(1)
elif args[:2] == ["task", "note"]:
    item = items[args[2]]
    claiming = "claims it" in args[3]
    # FAKE_SD_RACE: another writer adds followup 78 just before this write.
    race = os.environ.get("FAKE_SD_RACE")
    if race == "note" and not claiming and not state.get("raced"):
        state["raced"] = True
        item["notes"].append({"id": 78, "kind": "followup", "session": "cron", "body": "failed again",
                              "resolved_at": None})
        item["revision"] += 1
    if flag("--if-revision") not in (None, str(item["revision"])):
        save(); print("refused: the item changed", file=sys.stderr); sys.exit(1)
    item["notes"].append({"id": 900 + len(item["notes"]), "kind": "comment", "session": "login",
                          "body": args[3], "resolved_at": None})
    item["revision"] += 1
    save()
    # FAKE_SD_LOSE: the claim note commits, then its answer is lost.
    if os.environ.get("FAKE_SD_LOSE") and claiming:
        print("connection reset", file=sys.stderr); sys.exit(1)
    # FAKE_SD_CRASH: the claim note commits, then the process that wrote it dies.
    if os.environ.get("FAKE_SD_CRASH") and claiming:
        import signal; os.kill(os.getppid(), signal.SIGKILL); sys.exit(1)
    if "--json" in args:
        print(json.dumps(show(args[2])))
elif args[:2] == ["task", "edit"]:
    item = items[args[2]]
    if flag("--if-revision") not in (None, str(item["revision"])):
        save(); print("refused: the item changed", file=sys.stderr); sys.exit(1)
    item["parked_at"] = None if "--unpark" in args else "2026-11-17T15:40:00+00:00"
    item["revision"] += 1
elif args[:2] == ["reports", "acknowledge"]:
    item = items[args[2]]
    if os.environ.get("FAKE_SD_RACE") == "ack" and not state.get("raced"):
        state["raced"] = True
        item["notes"].append({"id": 78, "kind": "followup", "session": "cron", "body": "failed again",
                              "resolved_at": None})
        item["revision"] += 1
    if flag("--if-revision") not in (None, str(item["revision"])):
        save(); print("refused: the item changed", file=sys.stderr); sys.exit(1)
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
# CLAUDE_TOUCH: a rerun the model starts folds into the report it works on.
[ -z "$CLAUDE_TOUCH" ] || python3 -c 'import json,sys; p=sys.argv[1]; s=json.load(open(p)); s["items"][sys.argv[2]]["revision"] += 1; json.dump(s, open(p, "w"))' "$FAKE_SD_STATE" "$CLAUDE_TOUCH"
# CLAUDE_FAIL_AGAIN: the rerun fails, and its report adds followup 77 to alert 1.
[ -z "$CLAUDE_FAIL_AGAIN" ] || python3 -c 'import json,sys; p=sys.argv[1]; s=json.load(open(p)); i=s["items"]["1"]; i["revision"] += 1; i["notes"].append({"id": 77, "kind": "followup", "session": "cron", "body": "failed again", "resolved_at": None}); json.dump(s, open(p, "w"))' "$FAKE_SD_STATE"
[ -f "$CLAUDE_REPLY" ] && cat "$CLAUDE_REPLY"
exit "${CLAUDE_RC:-0}"
"""


def report(host, status="planning", followups=(), session="cron"):
    """A report; its followups are the ingest's (session `cron`) unless `session` says otherwise."""
    return {"host": host, "status": status, "title": f"a job on {host}: needs attention", "revision": 1,
            "notes": [{"id": n, "kind": "followup", "session": session, "body": f"followup {n}",
                       "resolved_at": None} for n in followups]}


def pinned_pack():
    pack = Path(os.environ.get("SD_ACCEPTANCE_PACK") or FOLDER.parent / ".ci" / "pack")
    if not (pack / "bin/sd").exists():
        raise AssertionError(f"no pinned pack at {pack}: run make test SUITE=night-triage, which fetches "
                             "it into .ci/pack, or set SD_ACCEPTANCE_PACK")
    return pack


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

    def run_job(self, rc=0, **env):
        return subprocess.run(["sh", str(self.folder / "night-triage.sh"), "run"], capture_output=True, text=True,
                              env=dict(self.env, CLAUDE_RC=str(rc), **env), timeout=60)

    def items(self):
        return json.loads(self.state.read_text())["items"]

    def comments(self, key):
        """The job's verdict notes: claim and release notes left out."""
        return [n["body"] for n in self.items()[key]["notes"] if n["kind"] == "comment"
                and not n["body"].startswith("night-triage on node1 claims it")
                and "claim released" not in n["body"]]

    def notes(self, key, text):
        return [n["body"] for n in self.items()[key]["notes"] if text in n["body"]]

    def left_claim(self, row="1 800"):
        """An alert a crashed run claimed: its claim note is this job's latest note."""
        alert = report("node1")
        alert["revision"] = 5
        alert["notes"] = [{"id": 800, "kind": "comment", "session": "login", "resolved_at": None,
                           "body": "night-triage on node1 claims it: an unattended triage run"}]
        (self.folder / "state").mkdir()
        (self.folder / "state/claims").write_text(row + "\n")
        return alert

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

    def test_a_hub_session_handoff_is_filed_as_a_followup_not_an_operator_note(self):
        # sd:8969: 23 of 41 operator handoffs asked for a delete, restart or
        # rerun a hub session later did; the prompt routes those to a
        # followup item and the existing `filed` path settles it.
        prompt = (FOLDER / "prompt.md").read_text()
        self.assertIn("--kind followup --ref hub:<host>-<slug>", prompt)
        self.assertIn("restart the machine, a decision, a login, money", prompt)
        self.given({"1": report("node1")}, [
            {"id": 1, "outcome": "filed", "summary": "delete the landed branch b1", "item": "sd:9"}])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.items()["1"]["status"], "planning")
        self.assertEqual(self.comments("1"),
                         ["night-triage on node1: filed as sd:9: delete the landed branch b1"])

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

    def parked(self, on, runs=1, recorded=1):
        """An alert an earlier run parked to wait on job-1 on `on`, with the
        job's run count `recorded` then and `runs` now."""
        alert = dict(report("node1"), parked_at="2026-11-16T10:00:00+00:00", runs=runs)
        alert["notes"] = [{"id": 700, "kind": "comment", "session": "login", "resolved_at": None,
                           "body": f"night-triage on node1: waits until {on} on job-1: a weekly fix; "
                                   f"parked until then (runs: {recorded})."}]
        return alert

    def test_a_date_in_the_summary_never_becomes_the_wake_date(self):
        # sd:9243 review: a greedy parse took `on 2026-02-30:` from the
        # summary, and every later scan died on the impossible date.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import night_triage
        on = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
        note, then = night_triage.verdict(
            {"runs": 1}, {"outcome": "waiting", "on": on, "run": "job-1",
                          "summary": "the vendor says on 2026-02-30: maybe"}, "node1")
        self.assertEqual(then, night_triage.PARK)
        self.assertIsNone(night_triage.wake({"fields": "{}"}, [{"body": note}], "node1"))

    def test_a_wait_never_parks_an_alert_with_a_persons_followup(self):
        # sd:9243 lane review: a park hid an operator's open followup.
        alert = report("node1", followups=[41], session="login")
        self.given({"1": alert}, [
            {"id": 1, "outcome": "waiting", "summary": "later", "run": "job-1", "on": day(3)}])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.items()["1"].get("parked_at"))
        self.assertIn("not parked: followups a person wrote", self.comments("1")[0])

    def test_a_wait_naming_another_job_does_not_park(self):
        # sd:9243 lane review: any run name and date parked the alert.
        sys.path.insert(0, str(FOLDER))
        import night_triage
        note, then = night_triage.verdict(
            {"runs": 1, "job": "job-1"},
            {"outcome": "waiting", "on": day(3), "run": "other-job", "summary": "s"}, "node1")
        self.assertFalse(then)
        self.assertIn("not this alert's job job-1", note)

    def test_a_parked_alert_is_skipped(self):
        # sd:9243: an alert parked by hand, or waiting on a date to come with
        # no new failure, is left alone.
        alerts = {"1": dict(report("node1"), parked_at="2026-11-16T10:00:00+00:00"),
                  "2": self.parked(day(3))}
        self.given(json.loads(json.dumps(alerts)), [])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.items(), alerts)
        self.assertIn("sd:1 is parked; skipped", result.stderr)
        self.assertIn("sd:2 is parked; skipped", result.stderr)

    def test_a_wait_whose_date_passed_is_unparked_and_triaged(self):
        # sd:9243 review: the park wakes once the dated run should have run.
        self.given({"1": self.parked(day(-1))}, [
            {"id": 1, "outcome": "filed", "summary": "the weekly run failed too", "item": "sd:77"}])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.items()["1"]["parked_at"])
        self.assertIn("filed as sd:77", self.comments("1")[-1])
        self.assertIn("unparked: the wait for job-1 on", self.comments("1")[1])

    def test_a_wait_whose_job_failed_again_is_unparked_and_triaged(self):
        # sd:9243 review: a repeat folds into the parked report and adds no
        # note; the run count it bumps wakes the park before the date.
        self.given({"1": self.parked(day(3), runs=2, recorded=1)}, [
            {"id": 1, "outcome": "operator", "summary": "s", "action": "a", "why": "w"}])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.items()["1"]["parked_at"])
        self.assertIn("unparked: the job failed again", self.comments("1")[1])

    def test_an_alert_waiting_on_a_dated_run_is_noted_and_parked(self):
        # sd:9243: nothing can settle it before a scheduled run on a known date.
        on = day(5)
        alert = report("node1", followups=[41])
        alert["runs"] = 3
        self.given({"1": alert}, [
            {"id": 1, "outcome": "waiting", "summary": "the fix lands with the weekly run",
             "run": "job-1", "on": on}])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        item = self.items()["1"]
        self.assertEqual(item["status"], "planning")
        self.assertIsNotNone(item.get("parked_at"))
        self.assertEqual(self.comments("1"), [
            f"night-triage on node1: waits until {on} on job-1: the fix lands with the weekly run; "
            "parked until then (runs: 3)."])
        self.assertIn("waiting", (self.tmp / "prompt").read_text())

    def test_a_compact_wait_date_is_written_so_the_wake_reads_it(self):
        # sd:9243 review: fromisoformat takes 20261117; the note must carry
        # YYYY-MM-DD, or the wake never reads it and the park never ends.
        on = day(2)
        self.given({"1": report("node1")}, [
            {"id": 1, "outcome": "waiting", "summary": "s", "run": "job-1", "on": on.replace("-", "")}])
        self.assertEqual(self.run_job().returncode, 0)
        note = self.comments("1")[0]
        self.assertIn(f"waits until {on} on job-1: s;", note)
        sys.path.insert(0, str(FOLDER))
        self.addCleanup(sys.path.remove, str(FOLDER))
        import night_triage
        alert = {"fields": json.dumps({"repeats": {"count": 2}})}
        self.assertEqual(night_triage.wake(alert, [{"body": note}], "node1"), "the job failed again")

    def test_a_wait_without_a_real_near_date_is_not_parked(self):
        # sd:9243 review: a shape-only date check parked a past or impossible
        # date; the wait must fall within the next 8 days.
        for on in ("soon", "2026-99-99", day(-1), day(9)):
            with self.subTest(on=on):
                self.given({"1": report("node1")}, [
                    {"id": 1, "outcome": "waiting", "summary": "later", "run": "job-1", "on": on}])
                self.assertEqual(self.run_job().returncode, 0)
                self.assertIsNone(self.items()["1"].get("parked_at"))
                self.assertIn("not settled", self.comments("1")[0])

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

    def test_a_rerun_that_touches_the_alert_still_settles_it(self):
        self.given({"1": report("node1")}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:7"}])
        result = self.run_job(CLAUDE_TOUCH="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.items()["1"]["status"], "planning")
        self.assertIn("filed as sd:7", self.comments("1")[0])
        self.assertFalse((self.folder / "state/claims").exists())

    def test_a_followup_added_during_the_run_blocks_the_acknowledge(self):
        self.given({"1": report("node1", followups=[41])}, [
            {"id": 1, "outcome": "fixed", "summary": "reran", "evidence": ["exit 0"],
             "followups": {"41": "the rerun passed"}}])
        result = self.run_job(CLAUDE_FAIL_AGAIN="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = self.items()["1"]
        self.assertEqual(item["status"], "planning")
        self.assertIsNone(next(n for n in item["notes"] if n["id"] == 77)["resolved_at"])
        self.assertIn("open followups without evidence: #77", self.comments("1")[0])

    def test_a_report_is_claimed_by_a_note_not_a_status(self):
        self.given({"1": report("node1")}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:7"}])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(self.state.read_text())["log"]
        self.assertFalse([c for c in calls if c[:2] == ["task", "status"]], calls)
        self.assertEqual(len(self.notes("1", "night-triage on node1 claims it")), 1)
        self.assertIn('"id": 1', (self.tmp / "prompt").read_text())
        self.assertIn("filed as sd:7", self.comments("1")[0])

    def test_another_sessions_claim_note_is_skipped(self):
        alert = report("node1")
        alert["notes"].append({"id": 50, "kind": "comment", "session": "operator", "resolved_at": None,
                               "body": "a session claims it: debugging the job"})
        self.given({"1": alert}, [])
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.items()["1"], alert)
        self.assertIn("sd:1 is claimed; skipped", result.stderr)

    def test_a_claim_a_crashed_run_left_is_released_by_the_next_run(self):
        self.given({"1": self.left_claim()}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:77"}])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(len(self.notes("1", "did not finish; claim released")), 1)
        # Released, then claimed and settled by this run.
        self.assertIn("filed as sd:77", self.comments("1")[0])
        self.assertFalse((self.folder / "state/claims").exists())

    def test_a_pending_claim_row_a_crash_left_is_reconciled(self):
        self.given({"1": self.left_claim(row="1 -")}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:77"}])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(len(self.notes("1", "did not finish; claim released")), 1)
        self.assertIn("filed as sd:77", self.comments("1")[0])

    def test_a_left_claim_another_session_took_over_is_left_to_it(self):
        alert = self.left_claim()
        alert["notes"].append({"id": 801, "kind": "comment", "session": "operator", "resolved_at": None,
                               "body": "the operator claims it: debugging it"})
        self.given({"1": alert}, [])
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.comments("1"), ["the operator claims it: debugging it"])
        self.assertNotIn('"id": 1', (self.tmp / "prompt").read_text())
        self.assertFalse((self.folder / "state/claims").exists())

    def test_an_sd_outage_at_release_keeps_the_claim_and_fails(self):
        self.given({"1": self.left_claim()}, [])
        result = self.run_job(FAKE_SD_DOWN="task note")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.folder / "state/claims").read_text(), "1 800\n")
        # The next run, with sd back, releases it.
        self.run_job()
        self.assertEqual(len(self.notes("1", "did not finish; claim released")), 1)

    def test_an_sd_outage_while_settling_keeps_the_claim_and_fails(self):
        self.given({"1": report("node1")}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:7"}])
        # The outage starts after the claim: the model run turns sd's writes off.
        (self.bin / "claude").write_text(FAKE_CLAUDE.replace(
            'exit "${CLAUDE_RC:-0}"', 'touch "$FAKE_SD_STATE.down"; exit "${CLAUDE_RC:-0}"'))
        sd = (self.pack / "sd").read_text().replace(
            'if " ".join(args)', 'if os.path.exists(path + ".down") and args[:2] == ["task", "note"] or " ".join(args)')
        (self.pack / "sd").write_text(sd)
        result = self.run_job()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.comments("1"), [])
        self.assertTrue((self.folder / "state/claims").read_text().startswith("1 9"))

    def test_a_claim_whose_answer_is_lost_is_given_back_in_the_same_run(self):
        self.given({"1": report("node1")}, [])
        result = self.run_job(FAKE_SD_LOSE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.notes("1", "did not reach the run; claim released")), 1)
        self.assertFalse((self.folder / "state/claims").exists())

    def test_a_crash_after_the_claim_still_leaves_the_alert_released(self):
        self.given({"1": report("node1")}, [])
        result = self.run_job(FAKE_SD_CRASH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.notes("1", "claim released")), 1)
        self.assertFalse((self.folder / "state/claims").exists())

    def test_a_followup_added_before_the_note_is_judged_again(self):
        self.given({"1": report("node1", followups=[41])}, [
            {"id": 1, "outcome": "fixed", "summary": "reran", "evidence": ["exit 0"],
             "followups": {"41": "the rerun passed"}}])
        result = self.run_job(FAKE_SD_RACE="note")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = self.items()["1"]
        self.assertEqual(item["status"], "planning")
        self.assertIsNone(next(n for n in item["notes"] if n["id"] == 78)["resolved_at"])
        self.assertEqual(len(self.comments("1")), 1)
        self.assertIn("open followups without evidence: #78", self.comments("1")[0])

    def test_a_followup_added_before_the_acknowledge_is_not_resolved(self):
        self.given({"1": report("node1", followups=[41])}, [
            {"id": 1, "outcome": "fixed", "summary": "reran", "evidence": ["exit 0"],
             "followups": {"41": "the rerun passed"}}])
        result = self.run_job(FAKE_SD_RACE="ack")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = self.items()["1"]
        self.assertEqual(item["status"], "planning")
        self.assertIsNone(next(n for n in item["notes"] if n["id"] == 78)["resolved_at"])
        self.assertIn("not acknowledged: the report changed after the note", self.comments("1")[-1])

    def test_another_writer_during_the_run_leaves_the_alert_to_them(self):
        self.given({"1": report("node1")}, [
            {"id": 1, "outcome": "fixed", "summary": "reran", "evidence": ["exit 0"], "followups": {}}])
        writes = ("import json,sys; p=sys.argv[1]; s=json.load(open(p)); i=s['items']['1']; i['revision'] += 1; "
                  "i['notes'].append({'id': 990, 'kind': 'comment', 'session': 'operator', "
                  "'body': 'looking at it too', 'resolved_at': None}); json.dump(s, open(p, 'w'))")
        (self.bin / "claude").write_text(FAKE_CLAUDE.replace(
            'exit "${CLAUDE_RC:-0}"', f'python3 -c "{writes}" "$FAKE_SD_STATE"; exit "${{CLAUDE_RC:-0}}"'))
        self.assertEqual(self.run_job().returncode, 0)
        self.assertEqual(self.items()["1"]["status"], "planning")
        self.assertEqual(len(self.notes("1", "not settled: operator wrote on it during the run")), 1)

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

    def test_the_prompt_carries_the_alerts_earlier_triage_notes(self):
        # sd:8374: a run re-asked the operator about findings a note had
        # settled hours earlier; the model never saw that note.
        alert = report("node1", followups=[41])
        alert["notes"].insert(0, {"id": 30, "kind": "comment", "session": "login", "resolved_at": None,
                                  "body": "settled: the crash is a known upstream bug; no action"})
        self.given({"1": alert}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:7"}])
        self.run_job()
        prompt = (self.tmp / "prompt").read_text()
        self.assertIn("settled: the crash is a known upstream bug; no action", prompt)
        self.assertNotIn("claims it: an unattended triage run", prompt)
        self.assertIn("a later note settles", (FOLDER / "prompt.md").read_text())

    def test_a_failed_sd_today_fails_the_run_and_gives_the_claims_back(self):
        self.given({"1": report("node1")}, [{"id": 1, "outcome": "filed", "summary": "s", "item": "sd:7"}])
        result = self.run_job(FAKE_SD_DOWN="today")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.items()["1"]["status"], self.comments("1")), ("planning", []))
        self.assertFalse((self.tmp / "args").exists(), "the model ran without its Operations input")
        self.assertFalse((self.folder / "state/claims").exists())

    def test_a_followup_a_person_wrote_blocks_the_acknowledge(self):
        self.given({"1": report("node1", followups=[41], session="operator")}, [
            {"id": 1, "outcome": "fixed", "summary": "reran", "evidence": ["exit 0"],
             "followups": {"41": "it needs nothing"}}])
        self.assertEqual(self.run_job().returncode, 0)
        item = self.items()["1"]
        self.assertEqual(item["status"], "planning")
        self.assertIsNone(item["notes"][0]["resolved_at"])
        self.assertIn("followups a person wrote need a fix", self.comments("1")[0])
        self.assertIn("#41 (operator)", self.comments("1")[0])

    def test_the_ingest_session_matches_the_pinned_pack(self):
        sys.path.insert(0, str(FOLDER))
        self.addCleanup(sys.path.remove, str(FOLDER))
        import night_triage
        source = (pinned_pack() / "lib/sd_db/reporting.py").read_text()
        self.assertIn(f'INGEST_SESSION = "{night_triage.INGEST_SESSION}"', source)

    def test_every_sd_call_parses_with_the_pinned_pack(self):
        self.given({"1": report("node1", followups=[41]), "2": report("node1"), "3": report("node1"),
                    "4": self.parked(day(-1))}, [
            {"id": 1, "outcome": "fixed", "summary": "s", "evidence": ["e"], "followups": {"41": "ok"}},
            {"id": 2, "outcome": "filed", "summary": "s", "item": "sd:7"},
            {"id": 3, "outcome": "waiting", "summary": "s", "run": "job-3", "on": day(2)}])
        self.assertEqual(self.run_job(CLAUDE_TOUCH="2").returncode, 0)
        calls = json.loads(self.state.read_text())["log"]
        verbs = {" ".join(c[:2]) if c[0] != "today" else "today" for c in calls}
        self.assertEqual(verbs, {"reports list", "today", "task show", "task note",
                                 "reports acknowledge", "task edit"})
        pack = pinned_pack()
        check = ("import importlib.machinery as m, importlib.util as u, json, sys\n"
                 "sys.path[:0] = [sys.argv[1] + '/bin', sys.argv[1] + '/lib']\n"
                 "loader = m.SourceFileLoader('sd_cli', sys.argv[1] + '/bin/sd')\n"
                 "cli = u.module_from_spec(u.spec_from_loader('sd_cli', loader)); loader.exec_module(cli)\n"
                 "for call in json.loads(sys.stdin.read()): cli.build_parser().parse_args(call)\n")
        done = subprocess.run([sys.executable, "-c", check, str(pack)], input=json.dumps(calls),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_help_exits_zero_and_no_argument_exits_one(self):
        script = str(self.folder / "night-triage.sh")
        self.assertEqual(subprocess.run(["sh", script, "help"], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(["sh", script], capture_output=True).returncode, 1)


class RealPack(unittest.TestCase):
    """The job against the pinned pack's real `sd` and library (sd:8143 review).

    The fake `sd` cannot prove a call the pack accepts: a report refused
    `sd task status` there while the fake moved it. Here the report comes
    from the pack's own ingest, in a database under a temporary HOME, and
    only `claude` and `hostname` are fakes.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.pack = pinned_pack().resolve()
        sys.path.insert(0, str(self.pack / "lib"))
        self.addCleanup(sys.path.remove, str(self.pack / "lib"))
        from sd_db.database import local_path
        from sd_db.migrate import initialise
        self.database = local_path(self.tmp)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        initialise(self.database)
        shutil.copytree(FOLDER, self.tmp / "system/local-night-triage",
                        ignore=shutil.ignore_patterns("state", "tests", "__pycache__"))
        shutil.copytree(FOLDER.parent / "lib", self.tmp / "system/lib")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("claude", FAKE_CLAUDE), ("hostname", "#!/bin/sh\necho Node1\n")):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)

    def ingest(self, run="r1", hour="08"):
        from contextlib import closing
        from unittest import mock
        from sd_db import connect, reporting
        log = self.tmp / "job.log"
        log.write_text("boom\n")
        with closing(connect(self.database)) as connection, \
                mock.patch("sd_db.ship.socket.gethostname", return_value="node1"):
            state = reporting.ingest_log(connection, job="job-a", run_id=run, started=f"2026-11-08T{hour}:00:00Z",
                                         ended=f"2026-11-08T{hour}:01:00Z", exit_code=1, log_path=str(log), offset=0)
        return state["item"]["id"]

    def run_job(self, reply):
        (self.tmp / "reply").write_text(json.dumps({"alerts": reply}))
        env = dict(os.environ, HOME=str(self.tmp), PATH=f"{self.bin}:{os.environ['PATH']}",
                   SD_PACK_ROOT=str(self.pack), TMPDIR=str(self.tmp), CLAUDE_ARGS=str(self.tmp / "args"),
                   CLAUDE_STDIN=str(self.tmp / "prompt"), CLAUDE_REPLY=str(self.tmp / "reply"))
        return subprocess.run(["sh", str(self.tmp / "system/local-night-triage/night-triage.sh"), "run"],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_a_repeat_folded_into_a_parked_report_wakes_it(self):
        # sd:9243 review: the pack folds a repeat into the parked report;
        # the next run unparks it and triages it.
        item = self.ingest()
        result = self.run_job([{"id": item, "outcome": "waiting", "summary": "s", "run": "job-a", "on": day(2)}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(self.state(item)["item"]["parked_at"])
        self.assertEqual(self.ingest(run="r2", hour="09"), item)
        result = self.run_job([{"id": item, "outcome": "filed", "summary": "s", "item": "sd:7"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state(item)
        self.assertIsNone(state["item"]["parked_at"], result.stderr)
        self.assertTrue(any("filed as sd:7" in n["body"] for n in state["notes"]))

    def state(self, item):
        from contextlib import closing
        from sd_db import connect, workflow
        with closing(connect(self.database)) as connection:
            return workflow.item_state(connection, item)

    def test_a_fixed_report_is_claimed_noted_and_acknowledged(self):
        item = self.ingest()
        followup = next(n["id"] for n in self.state(item)["notes"] if n["kind"] == "followup")
        (self.tmp / "reply").write_text(json.dumps({"alerts": [
            {"id": item, "outcome": "fixed", "summary": "a stale lock; cleared and reran",
             "evidence": ["cron-jobs.sh run job-a: exit 0"], "followups": {str(followup): "the rerun passed"}}]}))
        env = dict(os.environ, HOME=str(self.tmp), PATH=f"{self.bin}:{os.environ['PATH']}",
                   SD_PACK_ROOT=str(self.pack), TMPDIR=str(self.tmp), CLAUDE_ARGS=str(self.tmp / "args"),
                   CLAUDE_STDIN=str(self.tmp / "prompt"), CLAUDE_REPLY=str(self.tmp / "reply"))
        result = subprocess.run(["sh", str(self.tmp / "system/local-night-triage/night-triage.sh"), "run"],
                                capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'"id": {item}', (self.tmp / "prompt").read_text())
        state = self.state(item)
        self.assertEqual(state["item"]["status"], "done", result.stderr)
        bodies = [n["body"] for n in state["notes"]]
        self.assertTrue(any(b.startswith("night-triage on node1 claims it") for b in bodies), bodies)
        self.assertTrue(any("Evidence: cron-jobs.sh run job-a: exit 0" in b for b in bodies), bodies)
        self.assertIsNotNone(next(n for n in state["notes"] if n["id"] == followup)["resolved_at"])

    def test_a_report_waiting_on_a_dated_run_is_parked(self):
        # sd:9243: the pack parks a report through `sd task edit --park`.
        item = self.ingest()
        (self.tmp / "reply").write_text(json.dumps({"alerts": [
            {"id": item, "outcome": "waiting", "summary": "fixed by the weekly run",
             "run": "job-a", "on": day(2)}]}))
        env = dict(os.environ, HOME=str(self.tmp), PATH=f"{self.bin}:{os.environ['PATH']}",
                   SD_PACK_ROOT=str(self.pack), TMPDIR=str(self.tmp), CLAUDE_ARGS=str(self.tmp / "args"),
                   CLAUDE_STDIN=str(self.tmp / "prompt"), CLAUDE_REPLY=str(self.tmp / "reply"))
        result = subprocess.run(["sh", str(self.tmp / "system/local-night-triage/night-triage.sh"), "run"],
                                capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state(item)
        self.assertEqual(state["item"]["status"], "planning")
        self.assertIsNotNone(state["item"]["parked_at"], result.stderr)


if __name__ == "__main__":
    unittest.main()
