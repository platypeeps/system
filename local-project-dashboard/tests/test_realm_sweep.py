"""The Work and Personal sweeps' runner (sd:5096, sd:5097): `python3 -m sd_dashboard.realm_sweep`.

What this slice promises: the run's allowlist reads and saves Gmail drafts, and names no tool that sends, forwards or
writes; the Personal run loads workspace-mcp alone; a run reads its account from the pack's config.json and its filters
from sweep.conf, files the brief its model wrote as `<day> - <Realm> sweep`, and records done; the next run reads since
the last done one; one run per realm at a time; a missing account, filter or brief fails, naming it.

`claude` and `sd` are stubs: the claude stub writes the brief the prompt names and prints `RESULT: ok`; the sd stub
records its calls. No test calls claude, Gmail, Drive or the workflow database.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DASHBOARD))

from sd_dashboard import realm_sweep  # noqa: E402

CLAUDE = r'''import json, os, re, sys
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
with open(os.environ["STUB_SEEN"], "a") as seen:
    seen.write(json.dumps(argv) + "\n")
mode = os.environ.get("STUB_MODE", "")
if mode != "nobrief":
    brief = re.search(r"^- Brief file: (.+)$", prompt, re.M).group(1)
    series = re.search(r"^series: (.+)$", prompt, re.M).group(1)
    open(brief, "w").write(f"---\nseries: {series}\n---\n\n## Status\n\nNothing is due.\n")
print("did the steps")
print("RESULT: failed, Mail: the stub says so" if mode == "fail" else "RESULT: ok")
'''
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
                    self.assertRegex(tool, r"^Bash\(sd (store items|task (show|note|add --kind personal|add --belongs-to /repos/example-work/a)):\*\)$")
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
        (root / "config" / "project-dashboard").mkdir(parents=True)
        self.conf("personal|mail|in:inbox -to:hoa@example.test\npersonal|skip-drives|Example HOA\n"
                  "work|mail|in:inbox\nwork|followups|~/repos/example-work/a\n")
        (root / "claude.py").write_text(CLAUDE)
        claude = root / "claude"
        claude.write_text(f"#!/bin/sh\nexec {sys.executable} {root / 'claude.py'} \"$@\"\n")
        claude.chmod(0o755)
        sd = root / "sd"
        sd.write_text(SD)
        sd.chmod(0o755)
        self.seen, self.calls = root / "seen.jsonl", root / "sd-calls"
        self.env = {"HOME": str(root / "home"), "PATH": "/usr/bin:/bin", "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "xdg"), "SYSTEM_TOOLS_CONFIG": str(root / "config"),
                    "CLAUDE_BIN": str(claude), "REALM_SWEEP_SD": str(sd), "PYTHONPATH": str(DASHBOARD),
                    "STUB_SEEN": str(self.seen), "STUB_SD_CALLS": str(self.calls)}

    def accounts(self, accounts):
        (self.root / "xdg" / "sd-ai-command-pack" / "config.json").write_text(json.dumps({"google": {"accounts": accounts}}))

    def conf(self, text):
        (self.root / "config" / "project-dashboard" / "sweep.conf").write_text(text)

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
        self.assertIn("- mail: in:inbox -to:hoa@example.test", prompt)
        self.assertIn("Bash(sd task add --kind personal:*)", argv[argv.index("--allowedTools") + 1])
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
        self.assertIn("Since: seven days ago", prompts[0])
        self.assertIn(f"Since: {first}.", prompts[1])
        self.assertIn(f"Since: {first}.", prompts[2])
        # The work followups belong to the sweep.conf checkout, its ~ expanded, so they show under Work.
        self.assertIn(f"Bash(sd task add --belongs-to {self.root / 'home'}/repos/example-work/a:*)",
                      self.prompts()[0][self.prompts()[0].index("--allowedTools") + 1])

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

    def test_a_missing_account_or_filter_fails_naming_it_and_asks_no_model(self):
        self.accounts({"personal": {"email": "me@example.test"}})
        self.assertEqual(self.sweep("run", "work").returncode, 1)
        self.assertIn("google.accounts.work.email", self.record("work")["reason"])
        self.accounts({"work": {"email": "work@example.test"}})
        self.conf("work|mail|in:inbox\n")
        self.assertEqual(self.sweep("run", "work").returncode, 1)
        self.assertIn("work|followups|<value>", self.record("work")["reason"])
        (self.root / "config" / "project-dashboard" / "sweep.conf").unlink()
        self.assertEqual(self.sweep("run", "work").returncode, 1)
        self.assertIn("sweep.conf.example", self.record("work")["reason"])
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
