"""The Work and Personal sweeps' runner (sd:5096, sd:5097): `python3 -m sd_dashboard.realm_sweep`.

What this slice promises: the run's allowlist reads and saves Gmail drafts, and names no tool that sends, forwards or
writes; the Personal run loads workspace-mcp alone; a run reads its filters from the workflow database's setting rows
(sd:5109) and its account from that database or the pack's config.json, files the brief its model wrote as
`<day> - <Realm> sweep`, and records done; the next run reads since the last done one; one run per realm at a time; a
missing account, filter or brief, or a database that does not answer, fails, naming it.

`claude` and `sd` are stubs: the claude stub writes the brief the prompt names and prints `RESULT: ok`; the sd stub
records its calls. The workflow database is a temporary one under the run's HOME. No test calls claude, Gmail or Drive.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DASHBOARD))

import sd_db  # noqa: E402
from sd_db import connect, initialise, writes  # noqa: E402
from sd_db.database import local_path  # noqa: E402

from sd_dashboard import realm_sweep  # noqa: E402

#: The sd_db this process imported, for the run's own PYTHONPATH: the copy the gate pins, not the live pack.
PACK_LIB = Path(sd_db.__file__).resolve().parent.parent

CLAUDE = r'''import json, os, re, sys
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
with open(os.environ["STUB_SEEN"], "a") as seen:
    seen.write(json.dumps(argv) + "\n")
mode = os.environ.get("STUB_MODE", "")
# "hang" names its pid in STUB_READY, then outlives any test: the test kills it.
if mode == "hang":
    import time
    open(os.environ["STUB_READY"] + ".partial", "w").write(str(os.getpid()))
    os.replace(os.environ["STUB_READY"] + ".partial", os.environ["STUB_READY"])
    time.sleep(120)
if mode != "nobrief":
    brief = re.search(r"^- Brief file: (.+)$", prompt, re.M).group(1)
    series = re.search(r"^series: (.+)$", prompt, re.M).group(1)
    open(brief, "w").write(f"---\nseries: {series}\n---\n\n## Status\n\nNothing is due.\n")
# STUB_COPY "<from>|<to>" copies the run's record as the model sees it, to show what a stop here would leave.
if os.environ.get("STUB_COPY"):
    import shutil
    shutil.copy(*os.environ["STUB_COPY"].split("|"))
print("did the steps")
# STUB_READ names the sources the reply says it read in full; "-" prints no READ line.
read = os.environ.get("STUB_READ", "mail, drive, notion, jira, github")
if read != "-":
    print(f"READ: {read}")
print("RESULT: failed, Mail: the stub says so" if mode == "fail" else "RESULT: ok")
'''


def window(started):
    """The first window's start for a source first attempted by the run that started at `started`."""
    return (datetime.datetime.fromisoformat(started) - datetime.timedelta(days=7)).isoformat(timespec="seconds")


SD = r'''#!/bin/sh
printf '%s\n' "$*" >> "$STUB_SD_CALLS"
case "$1 $2 $3" in
  "reports brief show") [ -n "$STUB_SHOW" ] && { printf '%s\n' "$STUB_SHOW"; exit 0; }
                        [ -n "$STUB_SHOW_FAILS" ] && { echo "sd: database is locked" >&2; exit 1; }
                        echo "sd: no brief has the stem '$4'" >&2; exit 1 ;;
  "reports brief add") [ -n "$STUB_ADD_FAILS" ] && { echo "sd: example.test is not a registered repository" >&2; exit 1; }
                       echo "filed brief" ;;
esac
'''
#: Verbs a read tool's name carries, and verbs a write tool's name carries, split on `_` and `-`.
READ_VERBS = {"get", "search", "list", "read", "fetch"}
WRITE_VERBS = {"send", "forward", "reply", "draft", "create", "update", "modify", "manage", "delete", "add", "remove",
               "transition", "merge", "push", "write", "move", "duplicate", "import", "copy", "set", "insert",
               "replace", "edit", "assign", "submit", "dismiss", "close", "comment", "label", "trash", "upload"}


class Allowlist(unittest.TestCase):
    def test_every_mcp_tool_but_the_draft_reads_and_none_sends_or_writes(self):
        for realm in realm_sweep.REALMS:
            allowed = realm_sweep.allowed_tools(realm, Path("/runs/x"), "/repos/example-work/a")
            mcp = [tool.split("__", 2)[2] for tool in allowed if tool.startswith("mcp__")]
            self.assertEqual([t for t in allowed if t.endswith("__draft_gmail_message")],
                             ["mcp__workspace-mcp__draft_gmail_message"], realm)
            for name in mcp:
                if name == "draft_gmail_message":
                    continue
                words = set(re.split(r"[_-]", name))
                self.assertTrue(words & READ_VERBS, f"{realm}: {name} names no read verb")
                self.assertFalse(words & WRITE_VERBS, f"{realm}: {name} names a write verb")
            self.assertFalse(set(allowed) & set(realm_sweep.DENIED), realm)
            self.assertIn("mcp__workspace-mcp__send_gmail_message", realm_sweep.DENIED)
            self.assertFalse([t for t in allowed if re.search(r"send|forward", t)], realm)
            # Bash runs sd's item verbs only; Read and Write reach the run folder alone.
            for tool in allowed:
                if tool.startswith("Bash("):
                    self.assertRegex(tool, r"^Bash\(sd (store items|task (show|note|add --kind followup --no-repo|add --belongs-to /repos/example-work/a)):\*\)$")
                elif not tool.startswith("mcp__"):
                    self.assertIn(tool, ("Read(//runs/x/**)", "Write(//runs/x/**)"))

    def test_the_work_run_reads_notion_jira_and_github_and_the_personal_run_does_not(self):
        work = realm_sweep.allowed_tools("work", Path("/runs/x"), "/r")
        personal = realm_sweep.allowed_tools("personal", Path("/runs/x"))
        for server in ("notion", "jira", "github"):
            self.assertTrue([t for t in work if t.startswith(f"mcp__{server}__")], server)
            self.assertFalse([t for t in personal if t.startswith(f"mcp__{server}__")], server)

    def test_the_run_never_skips_permissions_and_the_personal_run_loads_workspace_mcp_alone(self):
        for realm in realm_sweep.REALMS:
            argv = realm_sweep.claude_argv("claude", realm, "go", "http://127.0.0.1:1/mcp", ["Read"])
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertFalse([a for a in argv if "skip-permissions" in a or a == "bypassPermissions"])
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
            if realm == "personal":
                self.assertIn("--strict-mcp-config", argv)
                self.assertEqual(list(json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]), ["workspace-mcp"])
            else:
                self.assertNotIn("--strict-mcp-config", argv)


class Run(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        (root / "home").mkdir()
        (root / "xdg" / "sd-ai-command-pack").mkdir(parents=True)
        self.accounts({"personal": {"email": "me@example.test"}, "work": {"email": "work@example.test"}})
        database = local_path(root / "home")
        database.parent.mkdir(parents=True)
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)
        self.set(**{"sweep.personal.mail_query": "in:inbox -to:hoa@example.test", "sweep.personal.drive_exclude": "Example HOA",
                    "sweep.work.mail_query": "in:inbox", "sweep.work.followups_repo": "~/repos/example-work/a"})
        (root / "claude.py").write_text(CLAUDE)
        claude = root / "claude"
        claude.write_text(f"#!/bin/sh\nexec {sys.executable} {root / 'claude.py'} \"$@\"\n")
        claude.chmod(0o755)
        sd = root / "sd"
        sd.write_text(SD)
        sd.chmod(0o755)
        self.seen, self.calls = root / "seen.jsonl", root / "sd-calls"
        self.env = {"HOME": str(root / "home"), "PATH": "/usr/bin:/bin", "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "xdg"), "CLAUDE_BIN": str(claude), "REALM_SWEEP_SD": str(sd),
                    "PYTHONPATH": f"{DASHBOARD}{os.pathsep}{PACK_LIB}",
                    "STUB_SEEN": str(self.seen), "STUB_SD_CALLS": str(self.calls)}

    def accounts(self, accounts):
        (self.root / "xdg" / "sd-ai-command-pack" / "config.json").write_text(json.dumps({"google": {"accounts": accounts}}))

    def set(self, **rows):
        for key, value in rows.items():
            writes.set_setting(self.connection, key, value)

    def state(self, realm):
        return self.root / "state" / "system" / f"{realm}-sweep"

    def sweep(self, *args, **env):
        return subprocess.run([sys.executable, "-m", "sd_dashboard.realm_sweep", *args], cwd=self.root,
                              env={**self.env, **env}, capture_output=True, text=True, timeout=60)

    def record(self, realm):
        return json.loads((self.state(realm) / "last.json").read_text())

    def prompts(self):
        return [json.loads(line) for line in self.seen.read_text().splitlines()] if self.seen.exists() else []

    def sd_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_a_run_files_its_brief_and_records_done(self):
        done = self.sweep("run", "personal")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        [argv] = self.prompts()
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("Google account: me@example.test", prompt)
        self.assertIn("- mail_query: in:inbox -to:hoa@example.test", prompt)
        self.assertIn("- drive_exclude: Example HOA", prompt)
        self.assertIn("Bash(sd task add --kind followup --no-repo:*)", argv[argv.index("--allowedTools") + 1])
        record = self.record("personal")
        self.assertEqual((record["state"], record["exit"]), ("done", 0))
        self.assertRegex(Path(record["brief"]).name, r"^\d{4}-\d{2}-\d{2} - Personal sweep\.md$")
        stem = Path(record["brief"]).stem
        self.assertEqual(self.sd_calls(), [f"reports brief show {stem}",
                                           f"reports brief add --file {record['brief']} --belongs-to "
                                           f"{realm_sweep.REPO} --replace"])
        status = json.loads(self.sweep("status", "personal", "--json").stdout)
        self.assertEqual((status["state"], status["running"]), ("done", False))

    def test_the_next_run_reads_since_the_last_done_run_and_a_failed_run_does_not_move_it(self):
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        first = self.record("work")["started"]
        self.assertEqual(self.sweep("run", "work", STUB_MODE="fail").returncode, 1)
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        prompts = [argv[argv.index("-p") + 1] for argv in self.prompts()]
        self.assertEqual(self.since(prompts[0], "mail"), window(first))
        self.assertIn(f"- mail: {first}\n", prompts[1])
        self.assertIn(f"- mail: {first}\n", prompts[2])
        # The work followups belong to the followups_repo setting's checkout, its ~ expanded, so they show under Work.
        self.assertIn(f"Bash(sd task add --belongs-to {self.root / 'home'}/repos/example-work/a:*)",
                      self.prompts()[0][self.prompts()[0].index("--allowedTools") + 1])

    def since(self, prompt, source):
        return re.search(rf"^- {source}: (.+)$", prompt.split("Since, by source", 1)[1], re.M).group(1)

    def test_a_source_the_run_did_not_read_keeps_its_checkpoint(self):
        """sd:5096 review: RESULT: ok with Jira unread must not move Jira's checkpoint past changes nobody read."""
        self.set(**{"sweep.work.jira_scope": "project in (ABC)"})
        self.assertEqual(self.sweep("run", "work", STUB_READ="Gmail, drive, `jira`.").returncode, 0)
        first = self.record("work")["started"]
        self.assertEqual(self.sweep("run", "work", STUB_READ="mail, drive").returncode, 0)
        second = self.record("work")
        self.assertEqual((second["state"], second["gaps"]), ("done", ["jira"]))
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        prompt = self.prompts()[2][self.prompts()[2].index("-p") + 1]
        self.assertEqual(self.since(prompt, "jira"), first)
        self.assertEqual(self.since(prompt, "mail"), second["started"])
        self.assertEqual(self.record("work")["gaps"], [])

    def test_a_newly_configured_source_reads_from_seven_days_back(self):
        """A source set after a done run has no checkpoint of its own; it must not take the others'."""
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        self.assertNotIn("github", self.record("work")["read"], "an unconfigured source counted as read")
        self.set(**{"sweep.work.github_scope": "org:example"})
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        prompt = self.prompts()[1][self.prompts()[1].index("-p") + 1]
        self.assertEqual(self.since(prompt, "github"), window(self.record("work")["started"]), prompt)
        self.assertEqual(self.since(prompt, "notion"), "not configured")

    def test_a_source_never_read_keeps_its_first_window_through_gaps_and_failures(self):
        """sd:5096 review round 2: Jira first tried on 2026-10-10 and back on 2026-10-18 must read from 2026-10-03.
        The first attempt writes that time to the record before the model runs; gaps and failures never recompute it."""
        self.set(**{"sweep.work.jira_scope": "project in (ABC)"})
        last, seen = self.state("work") / "last.json", self.root / "at-model.json"
        self.assertEqual(self.sweep("run", "work", STUB_READ="mail, drive", STUB_COPY=f"{last}|{seen}").returncode, 0)
        first = self.record("work")
        self.assertEqual(json.loads(seen.read_text())["since"]["jira"], window(first["started"]))
        self.assertIsNone(first["since"]["notion"], "an unconfigured source got a window")
        # That first attempt as if it ran on 2026-10-10; then a failed run, a run with Jira a gap, and the recovery.
        last.write_text(json.dumps({**first, "started": "2026-10-10T12:00:00+00:00",
                                    "since": {**first["since"], "jira": "2026-10-03T12:00:00+00:00"}}))
        self.assertEqual(self.sweep("run", "work", STUB_MODE="fail").returncode, 1)
        self.assertEqual(self.sweep("run", "work", STUB_READ="mail, drive").returncode, 0)
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        recovery = self.record("work")
        prompts = [argv[argv.index("-p") + 1] for argv in self.prompts()[1:]]
        self.assertEqual([self.since(prompt, "jira") for prompt in prompts], ["2026-10-03T12:00:00+00:00"] * 3)
        self.assertEqual((recovery["since"]["jira"], recovery["gaps"]), ("2026-10-03T12:00:00+00:00", []))
        # Read at last: the next run starts Jira from the recovery run's start.
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        self.assertEqual(self.record("work")["since"]["jira"], recovery["started"])

    def test_a_followup_ref_names_the_message_or_comment_not_its_thread(self):
        """sd:5096 review round 3: `--ref` refuses a second add whatever the first item's status, so a ref on a thread,
        file, issue or pull request drops every later action there. A ref names one message or comment, or a version;
        a draft is skipped only for the reply it already gives, not for its whole thread."""
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        prompt = self.prompts()[0][self.prompts()[0].index("-p") + 1]
        self.assertNotIn("thread id", prompt)
        for rule in ("a Gmail message id", "issue key and comment id", "comment or review id", "a Notion comment id",
                     "add `@` and its modified time", "adds `.2`, `.3`", "skip a reply a draft already gives"):
            self.assertIn(rule, prompt)
        self.assertNotIn("skip a thread that already has a draft", prompt)

    def test_the_personal_add_files_a_ref_and_refuses_the_same_ref_again(self):
        """sd:5096 review: the real sd takes `--ref` on a task or followup only; Personal's `--kind personal` add
        refused every followup. Personal's add, run by the pinned pack's sd, files once and refuses a repeat. Work's add
        is a task (sd's default kind), which takes a ref."""
        env = {**self.env, "SD_PACK_ROOT": str(PACK_LIB.parent), "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
        # The add the run may call, as its allowlist names it: `Bash(sd task add …:*)`.
        [add] = [tool[len("Bash(sd "):-len(":*)")].split() for tool in realm_sweep.allowed_tools("personal", Path("/runs/x"))
                 if tool.startswith("Bash(sd task add")]
        ref = "personal-sweep:gmail-m1"
        first, again = (subprocess.run([sys.executable, str(PACK_LIB.parent / "bin" / "sd"), *add, "--ref", ref, title],
                                       cwd=self.root, env=env, capture_output=True, text=True, timeout=60)
                        for title in ("Example", "Example again"))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertNotEqual(again.returncode, 0, again.stdout)
        self.assertIn(f"already carries --ref {ref}", again.stderr)

    def test_a_reply_without_a_read_line_moves_no_checkpoint(self):
        """A reply that names nothing it read is unknown: every source keeps its checkpoint, and the record says so."""
        self.assertEqual(self.sweep("run", "personal").returncode, 0)
        first = self.record("personal")["started"]
        self.assertEqual(self.sweep("run", "personal", STUB_READ="-").returncode, 0)
        self.assertEqual(self.record("personal")["gaps"], ["mail", "drive"])
        self.assertEqual(self.sweep("run", "personal").returncode, 0)
        prompt = self.prompts()[2][self.prompts()[2].index("-p") + 1]
        self.assertEqual((self.since(prompt, "mail"), self.since(prompt, "drive")), (first, first))

    def test_a_run_stopped_mid_way_or_left_by_the_older_record_keeps_every_checkpoint(self):
        """A run stopped between its record and its end leaves `running` with a dead pid; a record from before
        per-source checkpoints holds one `since` and no `read`. Neither may move a checkpoint to its start."""
        self.state("personal").mkdir(parents=True)
        last = self.state("personal") / "last.json"
        last.write_text(json.dumps({"state": "running", "pid": 99999999, "started": "2026-10-18T12:00:00+00:00",
                                    "since": {"mail": "2026-10-17T12:00:00+00:00", "drive": None}}))
        self.assertEqual(self.sweep("run", "personal").returncode, 0)
        last.write_text(json.dumps({"state": "done", "started": "2026-10-18T12:00:00+00:00",
                                    "since": "2026-10-16T12:00:00+00:00"}))
        self.assertEqual(self.sweep("run", "personal").returncode, 0)
        stopped, older = (argv[argv.index("-p") + 1] for argv in self.prompts())
        self.assertEqual(self.since(stopped, "mail"), "2026-10-17T12:00:00+00:00")
        drive = datetime.datetime.fromisoformat(self.since(stopped, "drive"))
        self.assertLess(abs(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7) - drive),
                        datetime.timedelta(minutes=5))
        self.assertEqual((self.since(older, "mail"), self.since(older, "drive")),
                         ("2026-10-16T12:00:00+00:00", "2026-10-16T12:00:00+00:00"))

    def test_drive_with_no_exclusions_is_read_not_skipped(self):
        """An unset drive_exclude excludes nothing; "not configured" would tell the model to skip Drive."""
        self.assertEqual(self.sweep("run", "work").returncode, 0)
        prompt = self.prompts()[0][self.prompts()[0].index("-p") + 1]
        self.assertIn("- drive_exclude: none", prompt)
        self.assertIn("drive", self.record("work")["read"])

    def test_a_second_run_of_a_realm_while_one_holds_the_lock_exits_3_and_writes_nothing(self):
        self.state("work").mkdir(parents=True)
        with open(self.state("work") / "sweep.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = self.sweep("run", "work")
            other = self.sweep("run", "personal")
        self.assertEqual(busy.returncode, 3, busy.stderr)
        self.assertIn("another work sweep is already running", busy.stderr)
        self.assertFalse((self.state("work") / "last.json").exists())
        self.assertEqual(other.returncode, 0, other.stderr)

    def test_a_runner_killed_mid_run_leaves_its_model_run_holding_the_lock(self):
        """sd:5341, as sd:5296 finding 1: the model run inherits the realm's lock, so a runner killed mid-run leaves
        no second run able to start beside its model, and status reads it running until the model ends."""
        ready = self.root / "ready"
        runner = subprocess.Popen([sys.executable, "-m", "sd_dashboard.realm_sweep", "run", "work"], cwd=self.root,
                                  env={**self.env, "STUB_MODE": "hang", "STUB_READY": str(ready)},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(runner.wait)
        self.addCleanup(runner.kill)
        deadline = time.monotonic() + 30
        while not ready.exists():
            self.assertLess(time.monotonic(), deadline, "the model run never started")
            time.sleep(0.05)
        model = int(ready.read_text())

        def stop():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(model, signal.SIGKILL)

        self.addCleanup(stop)
        runner.kill()
        runner.wait()
        busy = self.sweep("run", "work")
        self.assertEqual(busy.returncode, 3, busy.stderr)
        status = json.loads(self.sweep("status", "work", "--json").stdout)
        self.assertEqual((status["state"], status["running"]), ("running", True))
        self.assertIn("its model run still holds the lock", status["reason"])
        # The model gone, the lock is free: the record reads failed and the next run starts.
        stop()
        while self.sweep("status", "work", "--json").stdout.find('"running": true') >= 0:
            self.assertLess(time.monotonic(), deadline + 30, "the lock outlived the model run")
            time.sleep(0.05)
        self.assertEqual(json.loads(self.sweep("status", "work", "--json").stdout)["state"], "failed")
        self.assertEqual(self.sweep("run", "work").returncode, 0)

    def test_a_missing_account_or_filter_fails_naming_it_and_asks_no_model(self):
        self.accounts({"personal": {"email": "me@example.test"}})
        self.assertEqual(self.sweep("run", "work").returncode, 1)
        self.assertIn("google.accounts.work.email", self.record("work")["reason"])
        self.accounts({"work": {"email": "work@example.test"}})
        self.set(**{"sweep.work.followups_repo": None})
        self.assertEqual(self.sweep("run", "work").returncode, 1)
        self.assertEqual(self.record("work")["reason"], "sweep.work.followups_repo is not set; set it on Settings, under Sweeps")
        self.assertEqual(self.prompts(), [])

    def test_the_filters_are_database_rows_and_the_account_row_comes_before_config_json(self):
        """sd:5109: a changed row reaches the next prompt; no config file holds the filters."""
        self.set(**{"sweep.work.jira_scope": "project in (ABC)", "sweep.work.github_scope": "org:example",
                    "google.accounts.work.email": "row@example.test"})
        self.assertEqual(self.sweep("run", "work").returncode, 0, self.record("work"))
        prompt = self.prompts()[0][self.prompts()[0].index("-p") + 1]
        self.assertIn("- jira_scope: project in (ABC)", prompt)
        self.assertIn("- github_scope: org:example", prompt)
        self.assertIn("- notion_scope: not configured", prompt)
        self.assertIn("Google account: row@example.test", prompt)
        # A cleared account row is unset: config.json does not come back under it.
        self.set(**{"google.accounts.work.email": None})
        self.assertEqual(self.sweep("run", "work").returncode, 1)
        self.assertIn("the workflow database's google.accounts.work.email setting is unset", self.record("work")["reason"])

    def test_a_database_that_does_not_answer_fails_the_run_and_asks_no_model(self):
        """A failed read is unknown, not unset: the run stops before the model."""
        self.connection.close()
        local_path(self.root / "home").unlink()
        self.assertEqual(self.sweep("run", "personal").returncode, 1)
        self.assertIn("the personal sweep settings did not read from the workflow database", self.record("personal")["reason"])
        self.assertEqual(self.prompts(), [])

    def test_todays_filed_brief_reaches_the_prompt_and_a_failed_read_of_it_stops_the_run(self):
        """A second run in a day replaces the brief, so it must start from the one filed; a read that failed is not
        "none yet", and the run stops before the model rather than replace what it could not read."""
        self.assertEqual(self.sweep("run", "personal", STUB_SHOW="---\nseries: Personal sweep\n---\n\nMorning.").returncode, 0)
        prompt = self.prompts()[0][self.prompts()[0].index("-p") + 1]
        earlier = Path(self.record("personal")["brief"]).with_name("earlier.md")
        self.assertIn(f"Today's brief so far: {earlier}", prompt)
        self.assertIn("Morning.", earlier.read_text())
        self.assertEqual(self.sweep("run", "personal", STUB_SHOW_FAILS="1").returncode, 1)
        self.assertIn("today's brief was not read: sd: database is locked", self.record("personal")["reason"])
        self.assertEqual(len(self.prompts()), 1)

    def test_a_reply_without_a_brief_fails_and_files_nothing(self):
        self.assertEqual(self.sweep("run", "personal", STUB_MODE="nobrief").returncode, 1)
        self.assertIn("wrote no brief", self.record("personal")["reason"])
        self.assertFalse([call for call in self.sd_calls() if call.startswith("reports brief add")])

    def test_a_brief_sd_refuses_fails_the_run_and_stays_in_the_run_folder(self):
        self.assertEqual(self.sweep("run", "personal", STUB_ADD_FAILS="1").returncode, 1)
        record = self.record("personal")
        self.assertEqual(record["state"], "failed")
        self.assertIn("the brief was not filed: sd: example.test is not a registered repository", record["reason"])
        self.assertTrue(Path(record["brief"]).is_file())

    def test_a_running_record_whose_pid_is_gone_reads_failed(self):
        self.state("work").mkdir(parents=True)
        (self.state("work") / "last.json").write_text(json.dumps({"state": "running", "pid": 99999999, "log": "x.log"}))
        status = json.loads(self.sweep("status", "work", "--json").stdout)
        self.assertEqual((status["state"], status["running"]), ("failed", False))
        self.assertEqual(json.loads(self.sweep("status", "personal", "--json").stdout)["state"], "never")


if __name__ == "__main__":
    unittest.main()
