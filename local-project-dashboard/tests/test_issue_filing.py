"""File issue (sd:5103): a tool-less claude drafts a sanitized GitHub or Jira issue; only Confirm files it, once.

`claude` and `gh` are shell doubles named by `CLAUDE_BIN` and `GH_BIN`: each journals its argv and stdin, and the
claude double prints the reply the test set. Jira is a transport double handed to `file_jira`, or `FILERS` patched,
so nothing opens a socket. `HOME`, `XDG_STATE_HOME` and `SYSTEM_TOOLS_CONFIG` are disposable folders, the privacy
patterns are synthetic, and the real `local-leak-guard/leak-guard.sh scan` reads them. No test reaches a model,
GitHub, Jira or the live database.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from sd_db import repos, workflow, writes
from sd_dashboard import issue_filing, management_screen, tasks_screen
from sd_dashboard.issue_filing import REMOVED, findings, sanitize, scan

from test_workflow_actions import BrowserSession

SETTLE = 30
CANARY = "zebra-canary-4711"
PATTERNS = "# synthetic\n\nzebra-canary-[0-9]+\n^edge-start\nedge-end$\n"
CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$DOUBLE_DIR/claude.argv"
cat > "$DOUBLE_DIR/claude.stdin"
cat "$DOUBLE_DIR/reply"
exit "$(cat "$DOUBLE_DIR/exit" 2>/dev/null || echo 0)"
"""
GH = """#!/bin/sh
printf '%s\\n' "$@" >> "$DOUBLE_DIR/gh.argv"
cat > "$DOUBLE_DIR/gh.stdin"
echo call >> "$DOUBLE_DIR/gh.calls"
if [ -f "$DOUBLE_DIR/gh.fail" ]; then echo 'GraphQL: Could not resolve to a Repository' >&2; exit 1; fi
cat "$DOUBLE_DIR/gh.out"
"""
REPLY = ("Title: Export fails for sd:12 on a long name\n\nExporting a report whose name is longer than 200 characters "
         "fails with a 500.\n\nSeen in /Users/someone/repos/tool on host build-box.tail0000.ts.net, see #5103 and "
         "mail ops@example.test.\n\nExpected: the export finishes.")
TITLE = f"Export fails for {REMOVED} on a long name"


class FilingCase(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.root, self.double, self.home = root, root / "double", root / "home"
        self.double.mkdir()
        self.home.mkdir()
        self.config = root / "config"
        self.config.mkdir()
        (self.config / "privacy-patterns").write_text(PATTERNS)
        for name, text in (("claude", CLAUDE), ("gh", GH)):
            (root / name).write_text(text)
            (root / name).chmod(0o755)
        (self.double / "reply").write_text(REPLY)
        (self.double / "gh.out").write_text("Creating issue in example/tool\n\nhttps://github.com/example/tool/issues/42\n")
        environment = patch.dict(os.environ, {
            "CLAUDE_BIN": str(root / "claude"), "GH_BIN": str(root / "gh"), "DOUBLE_DIR": str(self.double),
            "HOME": str(self.home), "XDG_STATE_HOME": str(root / "state"), "SYSTEM_TOOLS_CONFIG": str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "LEAK_GUARD_PATTERNS"):
            os.environ.pop(name, None)
        self.checkout = root / "tool"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.checkout)], check=True, timeout=30)
        repos.upsert_repo(self.connection, str(self.checkout), remote="git@github.com:example/tool.git")
        repos.upsert_repo(self.connection, str(root / "secret"), remote="git@github.com:example/secret-plans.git")
        self.runs = root / "state" / "system" / "issue-filing" / "runs"

    def enable(self, github="on", jira="off"):
        repos.set_github_issues(self.connection, str(self.checkout), github)
        repos.set_jira_project(self.connection, str(self.checkout), jira)

    def task(self, title="Export fails on a long name", **columns):
        item = self.item(title, kind="task", repo=str(self.checkout), **columns)
        self.note(item, "Seen on the nightly run; branch fix/2026-10-18-export.", kind="comment")
        return item

    def revision(self, item):
        return workflow.item_state(self.connection, item)["revision"]

    def start(self, item, target="github"):
        return self.post(f"/api/items/{item}/file-issue", {"revision": self.revision(item), "target": target})

    def read(self, run_id):
        status, _, body = self.request(f"/api/issue-runs/{run_id}", headers={"Cookie": self.cookie})
        return status, json.loads(body)

    def settled(self, run_id):
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline:
            status, document = self.read(run_id)
            self.assertEqual(status, 200, document)
            if document["state"] not in ("queued", "drafting"):
                return document
            time.sleep(0.05)
        self.fail(f"run {run_id} did not settle in {SETTLE} s: {document}")

    def drafted(self, item, target="github"):
        status, _, run = self.start(item, target)
        self.assertEqual(status, 202, run)
        return self.settled(run["id"])

    def confirm(self, document, digest=None):
        return self.post(f"/api/issue-runs/{document['id']}/confirm", {"digest": digest or document["digest"]})

    def gh_calls(self):
        calls = self.double / "gh.calls"
        return len(calls.read_text().splitlines()) if calls.exists() else 0

    def item_row(self, item):
        return workflow.item_state(self.connection, item)


class TheSetting(FilingCase):
    """The per-repo setting, read and written through the library calls the verbs use."""

    def test_management_shows_and_writes_the_jira_project_and_refuses_a_stale_before(self):
        rows = {row["path"]: row for row in management_screen.document(self.connection, now=self.now)["repos"]}
        self.assertEqual(rows[str(self.checkout)]["jira_project"], "off")
        got = management_screen.set_repo(self.connection, "jira-project", str(self.checkout), "EXAMPLE", "off")
        self.assertEqual((got["field"], got["value"], got["before"]), ("jira_project", "EXAMPLE", "off"))
        self.assertEqual(repos.jira_project(self.connection, str(self.checkout)), "EXAMPLE")
        with self.assertRaises(management_screen.StaleSetting):
            management_screen.set_repo(self.connection, "jira-project", str(self.checkout), "OTHER", "off")
        rows = {row["path"]: row for row in management_screen.document(self.connection, now=self.now)["repos"]}
        self.assertEqual(rows[str(self.checkout)]["jira_project"], "EXAMPLE")

    def test_the_route_sets_a_key_clears_it_and_refuses_a_bad_key(self):
        body = {"path": str(self.checkout), "value": "EXAMPLE", "before": "off"}
        status, _, got = self.post("/api/repos/jira-project", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "EXAMPLE", "off"))
        self.assertEqual(self.post("/api/repos/jira-project", {**body, "value": "lower", "before": "EXAMPLE"})[0], 400)
        status, _, got = self.post("/api/repos/jira-project", {**body, "value": "off", "before": "EXAMPLE"})
        self.assertEqual((status, got["value"]), (200, "off"))
        self.assertIsNone(repos.jira_project(self.connection, str(self.checkout)))

    def test_tasks_rows_carry_each_target_or_why_it_is_off(self):
        item = self.task()
        row = {r["id"]: r for r in tasks_screen.document(self.connection, now=self.now)["rows"]}[item]
        self.assertFalse(row["issue_targets"]["github"]["ok"])
        self.assertIn("GitHub issues are off", row["issue_targets"]["github"]["why"])
        self.assertIn("no Jira project", row["issue_targets"]["jira"]["why"])
        self.assertIsNone(row["issue_link"])
        self.enable(github="on", jira="EXAMPLE")
        row = {r["id"]: r for r in tasks_screen.document(self.connection, now=self.now)["rows"]}[item]
        self.assertEqual(row["issue_targets"]["github"], {"ok": True, "target": {"kind": "github", "repo": "example/tool"},
                                                          "why": None})
        self.assertEqual(row["issue_targets"]["jira"]["target"], {"kind": "jira", "project": "EXAMPLE"})

    def test_an_off_target_refuses_the_start_and_drafts_nothing(self):
        item = self.task()
        status, _, answer = self.start(item, "github")
        self.assertEqual(status, 409)
        self.assertIn("GitHub issues are off", answer["error"])
        self.assertFalse((self.double / "claude.stdin").exists())


class TheSanitizer(FilingCase):
    CONTEXT = {"repositories": ["example/secret-plans"], "branches": ["fix/2026-10-18-export", "tool-export"],
               "hosts": ["build-box"], "home": "/Users/someone"}

    def test_every_listed_category_is_stripped_and_named_never_echoed(self):
        text = ("sd:12 sd#13 #5103 note 23220 ~/repos/x /Users/someone/a /Volumes/disk/b fix/2026-10-18-export "
                "tool-export build-box build-box.local box.tail0000.ts.net session_01ABCDEFGH "
                "https://claude.ai/code/session_x http://localhost:8765/tasks http://127.0.0.1:9/x http://intranet/a "
                "example/secret-plans https://github.com/example/secret-plans/pull/1 ops@example.test 192.0.2.7 "
                "ghp_abcdefghijklmnopqrstuvwxyz0123 file:///etc/hosts")
        clean = sanitize("A title", text, self.CONTEXT)
        self.assertEqual(clean["blocked"], [])
        for word in text.split():
            with self.subTest(word=word):
                self.assertNotIn(word, clean["body"])
        self.assertEqual(set(clean["removed"]), {"item reference", "local path", "branch name", "host name",
                                                 "session name", "internal URL", "private repository",
                                                 "email address", "IP address", "credential"})
        self.assertEqual(findings(clean["title"], clean["body"], self.CONTEXT), [])

    def test_nested_glued_and_bare_domain_forms_go_whole(self):
        """The forms the property test found: a removal inside `sd:` left `sd:`, a glued path kept `/Users`,
        `o@@example.test` kept `@example.test`, and a name glued to the word or letter before it was kept."""
        for text in ("sd:sd:12", "wordsd:12", "/Us/Users/ers/x", "/System/Volumes/Data/Users/a", "o@@example.test",
                     "wordsexample/secret-plans", "ébuild-box", "édgeb.local", "éfix/2026-10-18-x", "mysession_ABCDEFG1"):
            with self.subTest(text=text):
                body = sanitize("A title", f"Before {text} after", self.CONTEXT)["body"].lower()
                for raw in ("sd:", "/users", "/volumes", "@example", "secret-plans", "build-box", ".local", "2026-10-18",
                            "session_"):
                    self.assertNotIn(raw, body)

    def test_public_text_and_the_target_survive(self):
        text = "Run `tool export --name x` against https://github.com/example/tool/issues/3; a local file works."
        clean = sanitize("Export fails", text, self.CONTEXT)
        self.assertEqual((clean["body"], clean["removed"], clean["blocked"]), (text, {}, []))

    def test_property_no_rule_or_privacy_hit_survives_extreme_input(self):
        """Nested refs, unicode look-alikes and invisible characters, very long bodies, and a privacy pattern at every
        boundary: whatever is not blocked holds no rule finding and no pattern match, and a canary always blocks."""
        draw = random.Random(5103)
        fragments = ["sd:12", "sd:sd:12", "ｓｄ：１２", "s\u200bd:12", "sd\u2060:9", "#77", "##88", "note 4",
                     "/Users/someone/x", "/Us/Users/ers/x", "／Ｕｓｅｒｓ／a", "~/w", "~~/w", "/Volumes/v",
                     "fix/2026-10-18-export", "fix/fix/2026-10-18-exportx", "tool-export", "build-box",
                     "a.b.local", "x.tail1.ts.net", "localhost", "http://localhost:1/a", "http://[::1]:8/",
                     "ops@example.test", "o@@example.test", "192.0.2.1", "1922.0.2.1", "example/secret-plans",
                     "EXAMPLE/SECRET-PLANS", "session_ABCDEFG1", "\u202eevil\u202c", "é", "😀", "\x00", "\x1b[31m",
                     CANARY, "zebra-\u200bcanary-1", "ｚｅｂｒａ-canary-2", "edge-start", "edge-end", "plain words",
                     "https://github.com/example/tool/issues/9", "-", ".", "/", "@", ":", "\n", "\n\n", " ", "\t"]
        for case in range(250):
            size = draw.choice([1, 5, 40, 400])
            parts = [draw.choice(fragments) for _ in range(size)]
            glue = draw.choice(["", " ", "\n"])
            body = glue.join(parts)
            title = glue.join(draw.choice(fragments) for _ in range(draw.choice([1, 3, 8])))
            if case % 50 == 0:
                body = ("x" * 70000) + body + CANARY
            with self.subTest(case=case):
                clean = sanitize(title, body, self.CONTEXT)
                for reason in clean["blocked"]:
                    self.assertNotIn(CANARY, reason)
                    self.assertNotIn("someone", reason)
                if clean["blocked"]:
                    continue
                # The rules alone, before the privacy scan: a canary in the text must not hide what they let through.
                self.assertEqual(findings(clean["title"], clean["body"], self.CONTEXT), [])
                filed = clean["title"] + "\n" + clean["body"]
                # Glued with no space, a host or a dash name runs into its neighbour and is another word; spaced, it goes.
                spaced = ("ts.net", "build-box", "tool-export", "2026-10-18") if glue else ()
                for raw in ("sd:", "/Users", "someone", "@example", "secret-plans", "session_", "\u200b", "\u202e", *spaced):
                    self.assertNotIn(raw.lower(), filed.lower())
                blocked = scan(clean["title"], clean["body"])
                for reason in blocked:
                    self.assertNotIn(CANARY, reason)
                if not blocked:
                    for line in filed.splitlines():
                        self.assertIsNone(re.search(r"zebra-canary-[0-9]+|^edge-start|edge-end$", line), line)

    def test_a_megabyte_body_is_blocked_quickly_without_reading_it(self):
        started = time.monotonic()
        clean = sanitize("t", "a." * 500000, self.CONTEXT)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("longer than", clean["blocked"][0])

    def test_a_pattern_at_the_title_or_the_last_line_blocks_by_line_never_by_value(self):
        self.assertEqual(scan(f"{CANARY} title", "clean"), ["a privacy pattern matches the title"])
        self.assertEqual(scan("title", "one\ntwo\nlast edge-end"), ["a privacy pattern matches body line 3"])
        self.assertEqual(scan("title", "edge-start of a line"), ["a privacy pattern matches body line 1"])

    def test_no_pattern_file_blocks_rather_than_passes(self):
        (self.config / "privacy-patterns").unlink()
        self.assertIn("no privacy-pattern file", scan("title", "body")[0])


class TheDraft(FilingCase):
    def test_the_draft_runs_claude_with_no_tools_and_the_preview_is_the_sanitized_text(self):
        self.enable()
        item = self.task(title="$(touch pwned)")
        run = self.drafted(item)
        self.assertEqual(run["state"], "ready", run)
        self.assertEqual(run["title"], TITLE)
        for raw in ("sd:12", "/Users/someone", "ts.net", "#5103", "ops@example.test"):
            self.assertNotIn(raw, run["body"])
        self.assertEqual(run["target"], {"kind": "github", "repo": "example/tool"})
        self.assertEqual(run["digest"], issue_filing.digest(run["target"], run["title"], run["body"]))
        argv = (self.double / "claude.argv").read_text().split("\n")[:-1]
        self.assertEqual(argv, ["-p", "--model", "sonnet", "--restricted", "--strict-mcp-config", "--tools", "",
                                "--no-session-persistence"])
        stdin = (self.double / "claude.stdin").read_text()
        self.assertTrue(stdin.startswith(issue_filing.PROMPT))
        self.assertIn("Title: $(touch pwned)", stdin)
        self.assertFalse((self.root / "pwned").exists())
        for key in ("context", "claude", "guard", "pid", "server"):
            self.assertNotIn(key, run)

    def test_another_registered_repository_goes_by_its_full_and_its_bare_name(self):
        self.enable()
        (self.double / "reply").write_text("Title: Export fails\n\nIt also breaks secret-plans and example/secret-plans.\n")
        run = self.drafted(self.task())
        self.assertEqual(run["state"], "ready", run)
        self.assertNotIn("secret-plans", run["body"])
        self.assertEqual(run["removed"], {"private repository": 2})

    def test_reading_the_preview_never_files(self):
        self.enable()
        run = self.drafted(self.task())
        for _ in range(5):
            self.assertEqual(self.read(run["id"])[1]["state"], "ready")
        self.assertEqual(self.gh_calls(), 0)
        self.assertFalse((self.double / "gh.argv").exists())

    def test_a_privacy_pattern_in_the_draft_blocks_it_by_line(self):
        self.enable()
        (self.double / "reply").write_text(f"Title: Export fails\n\nFirst line.\nIt names {CANARY}.\n")
        run = self.drafted(self.task())
        self.assertEqual(run["state"], "blocked")
        self.assertEqual(run["blocked"], ["a privacy pattern matches body line 2"])
        self.assertNotIn(CANARY, json.dumps(run))
        self.assertEqual(self.confirm({"id": run["id"], "digest": "0" * 64})[0], 409)
        self.assertEqual(self.gh_calls(), 0)

    def test_a_model_failure_drafts_and_files_nothing(self):
        self.enable()
        (self.double / "exit").write_text("3")
        run = self.drafted(self.task())
        self.assertEqual(run["state"], "failed")
        self.assertIn("claude -p exited 3", run["reason"])
        self.assertEqual(self.gh_calls(), 0)

    def test_a_second_start_while_one_drafts_is_refused(self):
        self.enable()
        item = self.task()
        handle, _ = issue_filing._lock()
        self.addCleanup(handle.close)
        status, _, answer = self.start(item)
        self.assertEqual(status, 409)
        self.assertIn("already running", answer["error"])

    def test_a_runner_that_never_starts_or_stops_mid_draft_reads_failed_and_a_new_draft_starts(self):
        """Steps: stop between `queued` and `drafting`, and between `drafting` and the draft; the next read finds each
        failed, Confirm refuses it, Discard ends it, and nothing holds the lock against a new draft."""
        self.enable()
        item = self.task()
        self.runs.mkdir(parents=True)
        for n, (state, extra) in enumerate((("queued", {}), ("drafting", {"pid": 2 ** 22 + 12345}))):
            run_id = f"github-{item}-20261018T00000{n}Z"
            (self.runs / f"{run_id}.json").write_text(json.dumps({
                "id": run_id, "item": item, "kind": "github", "target": {"kind": "github", "repo": "example/tool"},
                "state": state, "started": "2026-10-18T00:00:00Z", **extra}))
            status, read = self.read(run_id)
            self.assertEqual((status, read["state"]), (200, "failed"), read)
            self.assertIn("nothing was filed", read["reason"])
            self.assertEqual(self.confirm({"id": run_id, "digest": "0" * 64})[0], 409)
            self.assertEqual(self.post(f"/api/issue-runs/{run_id}/discard", {})[0], 200)
        self.assertEqual(self.drafted(item)["state"], "ready")
        self.assertEqual(self.gh_calls(), 0)


class TheConfirm(FilingCase):
    def test_confirm_calls_gh_once_with_the_sanitized_text_and_links_back(self):
        self.enable()
        item = self.task()
        run = self.drafted(item)
        status, _, done = self.confirm(run)
        self.assertEqual(status, 200, done)
        self.assertEqual(self.gh_calls(), 1)
        argv = (self.double / "gh.argv").read_text().split("\n")[:-1]
        self.assertEqual(argv, ["issue", "create", "--repo", "example/tool", "--title", TITLE, "--body-file", "-"])
        self.assertEqual((self.double / "gh.stdin").read_text(), run["body"])
        self.assertEqual((done["state"], done["url"], done["ref"]),
                         ("linked", "https://github.com/example/tool/issues/42", "github:example/tool#42"))
        state = self.item_row(item)
        self.assertEqual((state["item"]["source"], state["item"]["external_id"]), ("task-ref", "github:example/tool#42"))
        self.assertEqual(state["notes"][-1]["body"],
                         "Filed GitHub issue github:example/tool#42: https://github.com/example/tool/issues/42")
        row = {r["id"]: r for r in tasks_screen.document(self.connection, now=self.now)["rows"]}[item]
        self.assertEqual(row["issue_link"], "https://github.com/example/tool/issues/42")
        self.assertEqual(self.confirm(run)[0], 409, "a linked run files again")
        self.assertEqual(self.gh_calls(), 1)

    def test_a_digest_of_other_text_is_refused_and_files_nothing(self):
        """Evidence: Confirm is tied to the previewed text and target, not to the run id alone."""
        self.enable()
        run = self.drafted(self.task())
        other = issue_filing.digest(run["target"], run["title"] + " changed", run["body"])
        status, _, answer = self.confirm(run, other)
        self.assertEqual(status, 409)
        self.assertIn("not the draft", answer["error"])
        self.assertEqual(self.gh_calls(), 0)

    def test_a_target_switched_off_or_moved_after_the_preview_refuses(self):
        self.enable(github="on")
        run = self.drafted(self.task())
        repos.set_github_issues(self.connection, str(self.checkout), "off")
        self.assertEqual(self.confirm(run)[0], 409)
        repos.set_github_issues(self.connection, str(self.checkout), "on")
        repos.upsert_repo(self.connection, str(self.checkout), remote="git@github.com:example/elsewhere.git")
        self.assertEqual(self.confirm(run)[0], 409)
        self.assertEqual(self.gh_calls(), 0)

    def test_a_pattern_added_after_the_preview_blocks_the_confirm(self):
        self.enable()
        run = self.drafted(self.task())
        (self.config / "privacy-patterns").write_text(PATTERNS + "long name\n")
        status, _, answer = self.confirm(run)
        self.assertEqual(status, 409)
        self.assertIn("privacy pattern matches the title", answer["error"])
        self.assertEqual(self.gh_calls(), 0)

    def test_a_gh_refusal_files_nothing_and_a_second_confirm_files_once(self):
        """Steps: a refused call returns the run to ready; the next Confirm finishes it."""
        self.enable()
        run = self.drafted(self.task())
        (self.double / "gh.fail").write_text("")
        status, _, answer = self.confirm(run)
        self.assertEqual(status, 502)
        self.assertIn("filed nothing", answer["error"])
        self.assertEqual(self.read(run["id"])[1]["state"], "ready")
        (self.double / "gh.fail").unlink()
        self.assertEqual(self.confirm(run)[0], 200)
        self.assertEqual(self.gh_calls(), 2)

    def test_a_server_gone_mid_filing_reads_unknown_and_confirm_refuses(self):
        """Steps: stop between `filing` and the answer; the next read finds it unknown, never files twice."""
        self.enable()
        run = self.drafted(self.task())
        record = self.runs / f"{run['id']}.json"
        document = json.loads(record.read_text())
        document.update(state="filing", server=2 ** 22 + 12345, filing_at=document["finished"])
        record.write_text(json.dumps(document))
        status, read = self.read(run["id"])
        self.assertEqual(read["state"], "unknown")
        self.assertIn("not known", read["reason"])
        self.assertEqual(self.confirm(run)[0], 409)
        self.assertEqual(self.gh_calls(), 0)
        self.assertEqual(self.post(f"/api/issue-runs/{run['id']}/discard", {})[0], 200)

    def test_an_answer_naming_another_repository_is_unknown_not_linked(self):
        self.enable()
        item = self.task()
        run = self.drafted(item)
        (self.double / "gh.out").write_text("https://github.com/example/secret-plans/issues/7\n")
        status, _, answer = self.confirm(run)
        self.assertEqual(status, 504)
        self.assertIsNone(self.item_row(item)["item"]["external_id"])

    def test_a_link_back_that_fails_is_retried_by_confirm_without_filing_again(self):
        """Steps: stop between the filing and the link-back; Confirm again links once and files nothing."""
        self.enable()
        item = self.task()
        run = self.drafted(item)
        with patch.object(issue_filing, "_link", side_effect=workflow.WorkflowError("database is locked")):
            status, _, answer = self.confirm(run)
        self.assertEqual(status, 503)
        self.assertIn("was filed", answer["error"])
        self.assertEqual(self.read(run["id"])[1]["state"], "filed")
        self.assertEqual(self.confirm(run)[0], 200)
        self.assertEqual(self.gh_calls(), 1)
        notes = [note["body"] for note in self.item_row(item)["notes"] if note["body"].startswith("Filed GitHub")]
        self.assertEqual(len(notes), 1)

    def test_an_item_with_its_own_source_keeps_it_and_gains_the_note(self):
        self.enable()
        item = self.task()
        writes.set_item_fields(self.connection, item, source="task-ref", external_id="job:repo-sync:4")
        run = self.drafted(item)
        self.assertEqual(self.confirm(run)[0], 200)
        state = self.item_row(item)
        self.assertEqual(state["item"]["external_id"], "job:repo-sync:4")
        self.assertIn("https://github.com/example/tool/issues/42", state["notes"][-1]["body"])

    def test_jira_confirm_posts_once_with_the_sanitized_text_and_links_the_key(self):
        self.enable(github="off", jira="EXAMPLE")
        item = self.task()
        run = self.drafted(item, "jira")
        self.assertEqual(run["target"], {"kind": "jira", "project": "EXAMPLE"})
        calls = []

        def transport(url, headers, body):
            calls.append((url, headers, body))
            return {"id": "1", "key": "EXAMPLE-9"}, ""

        env = {"JIRA_BASE_URL": "https://example.atlassian.test", "JIRA_EMAIL": "me@example.test",
               "JIRA_API_TOKEN": "not-a-token"}

        def filer(target, title, body):
            return issue_filing.file_jira(target, title, body, transport=transport, environ=env)

        with patch.dict(issue_filing.FILERS, {"jira": filer}):
            status, _, done = self.confirm(run)
        self.assertEqual(status, 200, done)
        self.assertEqual(len(calls), 1)
        url, headers, body = calls[0]
        self.assertEqual(url, "https://example.atlassian.test/rest/api/3/issue")
        self.assertEqual(body["fields"]["summary"], TITLE)
        self.assertEqual(body["fields"]["project"], {"key": "EXAMPLE"})
        text = "\n\n".join("".join(part.get("text", "\n") for part in paragraph["content"])
                           for paragraph in body["fields"]["description"]["content"])
        self.assertEqual(text, run["body"])
        self.assertEqual((done["url"], done["ref"]), ("https://example.atlassian.test/browse/EXAMPLE-9", "jira:EXAMPLE-9"))
        self.assertEqual(self.item_row(item)["item"]["external_id"], "jira:EXAMPLE-9")
        self.assertEqual(self.gh_calls(), 0)

    def test_jira_without_its_variables_files_nothing_and_names_them(self):
        target = {"kind": "jira", "project": "EXAMPLE"}
        with self.assertRaises(issue_filing.NotFiled) as caught:
            issue_filing.file_jira(target, "t", "b", transport=lambda *a: self.fail("a request left"), environ={})
        self.assertIn("JIRA_BASE_URL", str(caught.exception))

    def test_jira_status_errors_file_nothing_and_a_broken_connection_is_unknown(self):
        target = {"kind": "jira", "project": "EXAMPLE"}
        env = {"JIRA_BASE_URL": "https://example.atlassian.test", "JIRA_EMAIL": "e", "JIRA_API_TOKEN": "t"}
        with self.assertRaises(issue_filing.NotFiled):
            issue_filing.file_jira(target, "t", "b", transport=lambda *a: (None, "Jira returned 400 Bad Request"),
                                   environ=env)
        with self.assertRaises(issue_filing.Unknown):
            issue_filing.file_jira(target, "t", "b", transport=lambda *a: (None, "could not reach Jira: timeout"),
                                   environ=env)
        with self.assertRaises(issue_filing.Unknown):
            issue_filing.file_jira(target, "t", "b", transport=lambda *a: ({"key": "OTHER-1"}, ""), environ=env)
