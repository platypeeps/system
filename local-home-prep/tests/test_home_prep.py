"""The home prep job against a scratch database and a stub `claude` (sd:6029).

Each test builds a HOME of its own, initialises the workflow database there
with the pack's `bin/sd-db` (SD_PACK_ROOT, the pinned pack in the gate), files
items with the pack's `sd`, and puts a recording `claude` first on PATH.
Nothing reaches the real model, Gmail, launchd or the live database.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1]
SCRIPT = FOLDER / "home-prep.sh"
PACK = Path(os.environ.get("SD_PACK_ROOT") or Path.home() / "repos/platypeeps/sd-ai-command-pack")

# A `claude` that records its arguments, one per line in a file per call,
# then prints the result file and exits with CLAUDE_RC.
FAKE_CLAUDE = """#!/bin/sh
n=$(( $(cat "$CLAUDE_CALLS" 2>/dev/null | wc -l) + 1 ))
echo x >> "$CLAUDE_CALLS"
for arg in "$@"; do printf '%s\\0' "$arg"; done > "$CLAUDE_ARGS.$n"
[ -f "$CLAUDE_RESULT" ] && cat "$CLAUDE_RESULT"
exit "${CLAUDE_RC:-0}"
"""

# An `sd` that fails every call, for the read-failure rows.
FAILING_SD = "#!/bin/sh\necho 'sd: the hub is down' >&2\nexit 1\n"


def result(reply, **fields):
    """The object `claude -p --output-format json` prints, around a model reply."""
    found = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
             "num_turns": 3, "session_id": "s-1",
             "result": reply if isinstance(reply, str) else json.dumps(reply)}
    found.update(fields)
    return found


def prepped(*ids):
    return {"items": [{"id": item, "summary": f"Drafted the note for sd:{item}",
                       "checklist": ["Buy salt"], "drafts": ["Gmail draft: Salt order"],
                       "links": ["https://example.test/draft"]} for item in ids]}


class HomePrep(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="home-prep-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config = self.tmp / "config"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.result_file = self.tmp / "result"
        self.calls = self.tmp / "claude-calls"
        self.args = self.tmp / "claude-args"
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "CLAUDE_BIN", "WORKSPACE_MCP_URL")
                    and not key.startswith("HOME_PREP_")}
        self.env.update(HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}", SD_PACK_ROOT=str(PACK),
                        SYSTEM_TOOLS_CONFIG=str(self.config), HOME_PREP_ACCOUNT="operator@example.test",
                        CLAUDE_RESULT=str(self.result_file), CLAUDE_CALLS=str(self.calls),
                        CLAUDE_ARGS=str(self.args))
        self.pack("sd-db", "init")
        self.hoa = self.home / "repos" / "hoa"
        self.code = self.home / "repos" / "code"
        for checkout in (self.hoa, self.code):
            checkout.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            self.pack("sd-db", "repo", "add", str(checkout))
        self.env["HOME_PREP_REPOS"] = "~/repos/hoa"
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()

    def pack(self, program, *args, cwd=None):
        done = subprocess.run([str(PACK / "bin" / program), *args], env=self.env, capture_output=True, text=True,
                              timeout=60, cwd=cwd or self.home)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def add(self, title, *args, due=None):
        done = self.pack("sd", "task", "add", "--json", "--priority", "3", "--due", due or self.tomorrow, *args, title)
        return json.loads(done.stdout)["item"]["id"]

    def notes(self, item):
        shown = json.loads(self.pack("sd", "task", "show", "--json", str(item)).stdout)
        return [note["body"] for note in shown["notes"] if note["kind"] == "comment"]

    def run_job(self, *args, reply=None, rc=0, env=None):
        if reply is None:
            self.result_file.unlink(missing_ok=True)
        else:
            self.result_file.write_text(reply if isinstance(reply, str) else json.dumps(reply))
        return subprocess.run(["sh", str(SCRIPT), *(args or ("run",))], env=dict(self.env, CLAUDE_RC=str(rc),
                              **(env or {})), capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL)

    def model_runs(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def claude_args(self, call=1):
        return Path(f"{self.args}.{call}").read_text().split("\0")[:-1]

    def prompt(self, call=1):
        args = self.claude_args(call)
        return args[args.index("-p") + 1]

    def prompted(self, call=1):
        """The item ids the prompt asks the model to prep."""
        return sorted(int(line.split("sd:", 1)[1].split()[0]) for line in self.prompt(call).splitlines()
                      if line.startswith("- Item sd:"))

    def fake_sd(self):
        sd = self.tmp / "fake-pack" / "bin" / "sd"
        sd.parent.mkdir(parents=True)
        sd.write_text(FAILING_SD)
        sd.chmod(0o755)
        return {"SD_PACK_ROOT": str(sd.parent.parent)}

    # ------------------------------------------------------------ selection

    def test_selects_only_open_home_items_due_tomorrow(self):
        chore = self.add("Fill the softener with salt", "--no-repo", "--recur", "FREQ=MONTHLY")
        hoa = self.add("Board packet: draft the agenda mail", "--belongs-to", str(self.hoa))
        personal = self.add("Renew the car registration", "--kind", "personal")
        self.add("Code change in another repo", "--belongs-to", str(self.code))
        self.add("A chore due today", "--no-repo", due=date.today().isoformat())
        self.add("A chore due in two days", "--no-repo", due=(date.today() + timedelta(days=2)).isoformat())
        closed = self.add("A chore already done", "--no-repo")
        self.pack("sd", "task", "status", str(closed), "done", "--reason", "test")
        result_ = self.run_job(reply=result(prepped(chore, hoa, personal)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertEqual(self.prompted(), sorted([chore, hoa, personal]))
        self.assertIn("operator@example.test", self.prompt())

    def test_nothing_due_runs_no_model(self):
        self.add("Code change in another repo", "--belongs-to", str(self.code))
        result_ = self.run_job(reply=result(prepped()))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertEqual(self.model_runs(), 0)
        self.assertIn("nothing to prep", result_.stdout)

    def test_preview_lists_the_items_and_runs_no_model(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        result_ = self.run_job("preview", reply=result(prepped(chore)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertIn(f"sd:{chore}", result_.stdout)
        self.assertEqual(self.model_runs(), 0)
        self.assertEqual(self.notes(chore), [])

    def test_an_item_already_prepped_is_skipped(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        other = self.add("Clean the filter", "--no-repo")
        self.pack("sd", "task", "note", str(chore), f"Prepped for {self.tomorrow}: done by hand")
        result_ = self.run_job(reply=result(prepped(other)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertEqual(self.prompted(), [other])

    def test_a_note_for_an_earlier_due_does_not_count(self):
        chore = self.add("Fill the softener with salt", "--no-repo", "--recur", "FREQ=MONTHLY")
        earlier = (date.today() - timedelta(days=30)).isoformat()
        self.pack("sd", "task", "note", str(chore), f"Prepped for {earlier}: last month's")
        result_ = self.run_job(reply=result(prepped(chore)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertEqual(self.prompted(), [chore])

    # ------------------------------------------------------------ refusals before the model

    def test_a_missing_account_fails_before_the_model(self):
        self.add("Fill the softener with salt", "--no-repo")
        env = {key: value for key, value in self.env.items() if key != "HOME_PREP_ACCOUNT"}
        self.env = env
        result_ = self.run_job(reply=result(prepped()))
        self.assertEqual(result_.returncode, 1, result_.stdout + result_.stderr)
        self.assertIn("HOME_PREP_ACCOUNT is not set", result_.stderr)
        self.assertIn("local-home-prep/.env.example", result_.stderr)
        self.assertEqual(self.model_runs(), 0)

    def test_a_failed_item_read_fails_before_the_model(self):
        result_ = self.run_job(reply=result(prepped()), env=self.fake_sd())
        self.assertNotEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertIn("the hub is down", result_.stderr)
        self.assertEqual(self.model_runs(), 0)

    def test_a_failed_note_read_fails_before_the_model(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        items = self.pack("sd", "store", "items", "--open", "--json").stdout
        sd = self.tmp / "half-pack" / "bin" / "sd"
        sd.parent.mkdir(parents=True)
        listing = self.tmp / "items.json"
        listing.write_text(items)
        # `store items` answers; `task show` fails.
        sd.write_text(f"#!/bin/sh\nif [ \"$1\" = store ]; then cat '{listing}'; exit 0; fi\n" + FAILING_SD[10:])
        sd.chmod(0o755)
        result_ = self.run_job(reply=result(prepped(chore)), env={"SD_PACK_ROOT": str(sd.parent.parent)})
        self.assertNotEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertIn("the hub is down", result_.stderr)
        self.assertEqual(self.model_runs(), 0)

    # ------------------------------------------------------------ the model run and its judge

    def test_the_model_holds_no_send_tool_and_is_bounded(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        result_ = self.run_job(reply=result(prepped(chore)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        args = self.claude_args()
        flag = {name: args[args.index(name) + 1] for name in
                ("--permission-mode", "--max-turns", "--output-format", "--model", "--allowedTools", "--mcp-config")}
        self.assertEqual(flag["--permission-mode"], "dontAsk")
        self.assertEqual(flag["--output-format"], "json")
        self.assertEqual(flag["--model"], "sonnet")
        self.assertTrue(flag["--max-turns"].isdigit(), flag)
        self.assertIn("--restricted", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("mcp__workspace-mcp__draft_gmail_message", flag["--allowedTools"])
        for denied in ("send_gmail_message", "manage_drive_access", "set_drive_file_permissions",
                       "modify_gmail_message_labels", "Bash(sd task note", "Write", "Edit"):
            self.assertNotIn(denied, flag["--allowedTools"])
        self.assertIn("mcp__workspace-mcp__send_gmail_message", args[args.index("--disallowedTools") + 1])
        servers = json.loads(flag["--mcp-config"])["mcpServers"]
        self.assertEqual(list(servers), ["workspace-mcp"])

    def test_the_prompt_names_the_one_bash_command_the_run_holds(self):
        """dontAsk denies any other Bash call, and a denial fails the run."""
        chore = self.add("Fill the softener with salt", "--no-repo")
        result_ = self.run_job(reply=result(prepped(chore)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertIn("Bash(sd task show:*)", self.claude_args()[self.claude_args().index("--allowedTools") + 1])
        self.assertIn("Bash runs only `sd task show <n>`", self.prompt())
        self.assertIn("read files with Read, Grep and Glob", self.prompt())

    def test_a_run_writes_one_prepped_note_per_item(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        hoa = self.add("Board packet: draft the agenda mail", "--belongs-to", str(self.hoa))
        result_ = self.run_job(reply=result(prepped(chore, hoa)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        for item in (chore, hoa):
            notes = self.notes(item)
            self.assertEqual(len(notes), 1, notes)
            self.assertTrue(notes[0].startswith(f"Prepped for {self.tomorrow}: Drafted the note for sd:{item}"),
                            notes)
            self.assertIn("- Buy salt", notes[0])
            self.assertIn("Draft: Gmail draft: Salt order", notes[0])
            self.assertIn("Link: https://example.test/draft", notes[0])
        # The job never moves an item: the operator acts.
        shown = json.loads(self.pack("sd", "task", "show", "--json", str(chore)).stdout)
        self.assertEqual(shown["item"]["status"], "planning")

    def test_a_failed_model_run_writes_no_note_and_fails(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        result_ = self.run_job(reply=result(prepped(chore)), rc=1)
        self.assertNotEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertEqual(self.notes(chore), [])

    def test_the_result_object_judges_the_run_not_the_exit_code(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        for name, reply in (("max turns", result(prepped(chore), subtype="error_max_turns")),
                            ("is_error", result(prepped(chore), is_error=True)),
                            ("no result object", "the model said something"),
                            ("reply not JSON", result("I prepped it all, trust me"))):
            with self.subTest(name):
                result_ = self.run_job(reply=reply, rc=0)
                self.assertEqual(result_.returncode, 1, result_.stdout + result_.stderr)
                self.assertEqual(self.notes(chore), [])

    def test_a_permission_denial_fails_after_the_notes(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        denial = [{"tool_name": "mcp__workspace-mcp__send_gmail_message", "tool_use_id": "t1", "tool_input": {}}]
        result_ = self.run_job(reply=result(prepped(chore), permission_denials=denial))
        self.assertEqual(result_.returncode, 1, result_.stdout + result_.stderr)
        self.assertIn("mcp__workspace-mcp__send_gmail_message", result_.stderr)
        self.assertEqual(len(self.notes(chore)), 1)

    def test_a_reply_naming_an_unselected_item_writes_no_note_for_it(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        code = self.add("Code change in another repo", "--belongs-to", str(self.code))
        result_ = self.run_job(reply=result(prepped(chore, code)))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertEqual(self.notes(code), [])
        self.assertIn(f"sd:{code}", result_.stderr)
        self.assertEqual(len(self.notes(chore)), 1)

    def test_a_malformed_entry_writes_no_note_and_fails(self):
        good, drop = prepped(0)["items"][0], object()
        for name, fields in (("no summary", {"summary": drop}),
                             ("empty summary", {"summary": "  "}),
                             ("summary not text", {"summary": 7}),
                             ("checklist not a list", {"checklist": "Buy salt"}),
                             ("drafts not text", {"drafts": [{"subject": "Salt"}]}),
                             ("links null", {"links": None})):
            with self.subTest(name):
                # One item each: a note written by a failing row would hide the next.
                chore = self.add(f"Fill the softener with salt ({name})", "--no-repo")
                entry = {key: value for key, value in dict(good, id=chore, **fields).items() if value is not drop}
                result_ = self.run_job(reply=result({"items": [entry]}))
                self.assertEqual(result_.returncode, 1, result_.stdout + result_.stderr)
                self.assertIn(f"sd:{chore}", result_.stderr)
                self.assertEqual(self.notes(chore), [])

    def test_an_entry_without_the_optional_lists_is_prepped(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        result_ = self.run_job(reply=result({"items": [{"id": chore, "summary": "Nothing to buy"}]}))
        self.assertEqual(result_.returncode, 0, result_.stdout + result_.stderr)
        self.assertEqual(self.notes(chore), [f"Prepped for {self.tomorrow}: Nothing to buy"])

    def test_an_item_missing_from_the_reply_fails_the_run(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        other = self.add("Clean the filter", "--no-repo")
        result_ = self.run_job(reply=result(prepped(chore)))
        self.assertEqual(result_.returncode, 1, result_.stdout + result_.stderr)
        self.assertIn(f"sd:{other}", result_.stderr)
        self.assertEqual(len(self.notes(chore)), 1)
        self.assertEqual(self.notes(other), [])

    def test_a_rerun_after_some_notes_preps_only_the_rest(self):
        chore = self.add("Fill the softener with salt", "--no-repo")
        other = self.add("Clean the filter", "--no-repo")
        first = self.run_job(reply=result(prepped(chore)))
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        second = self.run_job(reply=result(prepped(other)))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.prompted(2), [other])
        self.assertEqual(len(self.notes(chore)), 1)
        self.assertEqual(len(self.notes(other)), 1)


if __name__ == "__main__":
    unittest.main()
