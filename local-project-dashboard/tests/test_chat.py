"""Chat (sd:5294): the right pane's question goes to one bounded, read-only `claude -p`, and its answer comes back.

`claude` is test_email_draft's shell double, named by `CLAUDE_BIN`: it journals its argv, stdin, working directory and
the token it saw, then prints the reply the test set. `HOME` is a disposable folder, so the operator's own
`~/.config/shell/env.sh` is never sourced and no test reaches claude.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import chat, email_draft

from test_email_draft import DOUBLE
from test_workflow_actions import BrowserSession

REPLY = "The next step is the review of sd:12.\n"
ARGV = ["-p", "--model", "sonnet", "--restricted", "--strict-mcp-config", "--tools", "Read,Grep,Glob",
        "--no-session-persistence"]


class ChatCase(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.double, self.home = root / "double", root / "home"
        self.double.mkdir()
        self.home.mkdir()
        claude = root / "claude"
        claude.write_text(DOUBLE)
        claude.chmod(0o755)
        (self.double / "reply").write_text(REPLY)
        environment = patch.dict(os.environ, {"CLAUDE_BIN": str(claude), "DOUBLE_DIR": str(self.double),
                                              "HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("DOUBLE_TOKEN", None)
        self.addCleanup(self.reap)

    def reap(self):
        for name in ("pid", "escaped"):
            try:
                os.kill(int((self.double / name).read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def ask(self, **body):
        return self.post("/api/chat", {"scope": "fleet", "page": "Today", "text": "What is next?", **body})

    def ran(self) -> bool:
        return (self.double / "argv").exists()

    def cwd(self) -> Path:
        return Path((self.double / "cwd").read_text().strip())

    def stdin(self) -> str:
        return (self.double / "stdin").read_text()


class TheRequest(ChatCase):
    def test_a_question_gets_the_answer_of_a_read_only_run_and_writes_nothing(self):
        before = self.snapshot()
        status, _, answer = self.ask()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer, {"answer": REPLY.strip(), "where": str(chat.CHECKOUT)})
        self.assertEqual((self.double / "argv").read_text().split("\n")[:-1], ARGV)
        self.assertEqual(self.cwd(), chat.CHECKOUT.resolve())
        stdin = self.stdin()
        self.assertTrue(stdin.startswith(chat.PROMPT), stdin)
        self.assertIn("Page: Today (scope: fleet)\n", stdin)
        self.assertTrue(stdin.endswith("Question:\nWhat is next?\n"), stdin)
        self.assertEqual(self.snapshot(), before)

    def test_the_token_is_sourced_from_env_sh_on_the_same_line(self):
        (self.home / ".config" / "shell").mkdir(parents=True)
        (self.home / ".config" / "shell" / "env.sh").write_text("export DOUBLE_TOKEN=from-env-sh\n")
        self.assertEqual(self.ask()[0], 200)
        self.assertEqual((self.double / "token").read_text(), "from-env-sh")

    def test_the_question_reaches_no_shell(self):
        status, _, _ = self.ask(text="$(touch pwned) `touch pwned2`")
        self.assertEqual(status, 200)
        self.assertIn("Question:\n$(touch pwned) `touch pwned2`\n", self.stdin())
        self.assertEqual(sorted(p.name for p in chat.CHECKOUT.glob("pwned*")), [])
        self.assertNotIn("pwned", (self.double / "argv").read_text())

    def test_a_post_without_the_csrf_token_runs_nothing(self):
        status, _, _ = self.post("/api/chat", {"text": "What is next?"}, **{"X-SD-CSRF": "0" * 64})
        self.assertEqual(status, 403)
        self.assertFalse(self.ran())

    def test_a_bad_body_is_refused_and_runs_nothing(self):
        for body in ({"text": ""}, {"text": "   "}, {"text": "x" * (chat.TEXT_MAX + 1)}, {"text": 7},
                     {"text": "hi", "model": "opus"}, {"scope": "../etc", "text": "hi"},
                     {"text": "hi", "selected": {"item": "12"}}, {"text": "hi", "selected": {"item": 0}},
                     {"text": "hi", "selected": {"label": "x" * (chat.LABEL_MAX + 1)}},
                     {"text": "hi", "selected": {"cmd": "rm"}}, {"text": "hi", "selected": []}):
            with self.subTest(body=body):
                status, _, answer = self.post("/api/chat", body)
                self.assertEqual(status, 400, answer)
        self.assertFalse(self.ran())


class TheContext(ChatCase):
    def test_the_selected_item_is_read_here_and_its_repo_is_the_directory(self):
        checkout = Path(self.tmp.name) / "other"
        checkout.mkdir()
        item = self.item("Plan the review", kind="task", repo=self.repo(str(checkout)))
        self.note(item, "Pat wants the plan by Friday.", kind="comment")
        selected = {"id": f"t{item}", "type": "item", "label": f"#{item} Plan", "item": item}
        status, _, answer = self.ask(page="Tasks", scope="tasks", selected=selected)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["where"], str(checkout))
        self.assertEqual(self.cwd(), checkout.resolve())
        stdin = self.stdin()
        self.assertIn(f"Selected row: item #{item} Plan (id t{item})\n", stdin)
        self.assertIn(f"# Selected item sd:{item}: Plan the review\n", stdin)
        self.assertIn("Pat wants the plan by Friday.", stdin)

    def test_an_item_whose_repo_is_gone_answers_503_and_runs_nothing(self):
        gone = str(Path(self.tmp.name) / "gone")
        item = self.item("Plan the review", kind="task", repo=self.repo(gone))
        status, _, answer = self.ask(scope="pack", selected={"id": "a", "type": "item", "label": "x", "item": item})
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], f"No answer: sd:{item} names {gone}, which is not a directory on this machine.")
        self.assertFalse(self.ran())

    def test_a_missing_item_answers_404_and_frees_the_lock(self):
        status, _, _ = self.ask(selected={"id": "a", "type": "item", "label": "x", "item": 999})
        self.assertEqual(status, 404)
        self.assertFalse(self.ran())
        self.assertEqual(self.ask()[0], 200)

    def test_the_pack_scope_runs_in_the_pack_checkout(self):
        pack = Path(self.tmp.name) / "pack"
        (pack / "lib").mkdir(parents=True)
        with patch.dict(os.environ, {"SD_PACK_ROOT": str(pack)}):
            status, _, answer = self.ask(scope="pack", page="Skills")
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.cwd(), pack.resolve())


class OneAtATime(ChatCase):
    def test_a_second_question_while_one_runs_is_refused(self):
        (self.double / "hang").write_text("")
        first = {}
        with patch.object(email_draft, "MODEL_TIMEOUT", 3):
            thread = threading.Thread(target=lambda: first.update(answer=self.ask()))
            thread.start()
            for _ in range(100):
                if (self.double / "pid").exists():
                    break
                time.sleep(0.05)
            status, _, answer = self.ask(text="And then?")
            thread.join()
        self.assertEqual(status, 409)
        self.assertEqual(answer["error"], "A chat answer is still running; ask again when it is shown.")
        self.assertEqual(first["answer"][0], 504)

    def test_a_timeout_kills_the_run_and_frees_the_lock(self):
        (self.double / "hang").write_text("")
        with patch.object(email_draft, "MODEL_TIMEOUT", 1):
            status, _, answer = self.ask()
        self.assertEqual(status, 504)
        self.assertEqual(answer["error"], "No answer: claude -p gave no answer in 1 s.")
        pid = int((self.double / "pid").read_text())
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"the run's child {pid} outlived the timeout")
        (self.double / "hang").unlink()
        self.assertEqual(self.ask()[0], 200)

    def test_the_run_ends_by_its_own_alarm_when_no_dashboard_waits(self):
        # A restart leaves the run without the runner that would kill it; the line's alarm still ends it.
        (self.double / "hang").write_text("")
        with patch.object(email_draft, "MODEL_TIMEOUT", 0), patch.object(chat, "GRACE", 1):
            run = subprocess.Popen(["sh", "-c", chat.line()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            code = run.wait(timeout=10)
        finally:
            if run.poll() is None:
                os.killpg(run.pid, signal.SIGKILL)
        self.assertEqual(code, -signal.SIGALRM)


class TheFailures(ChatCase):
    def test_a_failed_run_is_no_answer_and_names_its_last_line(self):
        (self.double / "exit").write_text("1")
        (self.double / "reply").write_text("Not logged in\n")
        status, _, answer = self.ask()
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], "No answer: claude -p exited 1: Not logged in.")

    def test_an_empty_reply_is_no_answer(self):
        (self.double / "reply").write_text("\n")
        status, _, answer = self.ask()
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], "No answer: claude -p answered nothing.")

    def test_a_long_answer_is_cut(self):
        (self.double / "reply").write_text("x" * (chat.ANSWER_MAX + 50))
        status, _, answer = self.ask()
        self.assertEqual(status, 200)
        self.assertEqual(len(answer["answer"]), chat.ANSWER_MAX)
        self.assertTrue(answer["answer"].endswith("…"))
