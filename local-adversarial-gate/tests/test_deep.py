"""`adversarial-gate deep`, against a fake `claude` named by `CLAUDE_BIN`.

sd:5100: the deep review runs headless claude on `lenses/deep.md`, read-only on
the checkout. What makes it read-only is the argv: a tool list with no edit,
write, shell or network tool, no MCP server, and a permission mode that denies
whatever the list leaves out. These cases read that argv back from a fake that
records it, so no test reaches the real CLI. The fake is reached through
`CLAUDE_BIN`, and HOME is a fixture folder, so the operator's env file is never
sourced.
"""

import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "adversarial-gate.sh"
RUN_TIMEOUT = 20
#: The tools that only read: the file readers, and Agent, whose sub-agents get the same set.
READ_ONLY = {"Read", "Grep", "Glob", "Agent"}
#: Tools that change files, run commands or reach the network. None may be offered.
WRITERS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell", "WebFetch", "WebSearch", "Task"}

FAKE = """#!/bin/sh
printf '%s\\n' "$@" > "$CLAUDE_ARGV"
pwd > "$CLAUDE_CWD"
cat > "$CLAUDE_STDIN"
case "$CLAUDE_MODE" in
  hang) sleep 60 & echo $! > "$CLAUDE_GRANDCHILD"; sleep 60 ;;
  fail) echo "error: not logged in" >&2; exit 2 ;;
esac
echo "### 1. A finding"
echo "Confidence: LIKELY"
"""


class Deep(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="adversarial-gate-deep."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.fake = self.tmp / "claude"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o755)
        self.out = self.tmp / "out" / "deep.md"
        self.out.parent.mkdir()
        self.log = {name: self.tmp / f"claude.{name}" for name in ("argv", "cwd", "stdin", "grandchild")}

    def gate(self, *args, mode="ok", claude=None):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp / "home"), "CLAUDE_BIN": str(claude or self.fake),
               "CLAUDE_MODE": mode, "ADVERSARIAL_GATE_JEV": str(self.tmp / "no-jev"),
               **{f"CLAUDE_{name.upper()}": str(path) for name, path in self.log.items()}}
        proc = subprocess.Popen(["sh", str(SCRIPT), "deep", "--lens", "deep", "--set", "REPO_NAME=fixture", "--set", "AGENTS=8", *args],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            self.fail(f"adversarial-gate deep was still running after {RUN_TIMEOUT}s")
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def argv(self):
        return self.log["argv"].read_text().splitlines()

    def value(self, flag):
        words = self.argv()
        return words[words.index(flag) + 1]

    def test_the_tool_allowlist_holds_no_write_tool(self):
        result = self.gate("--repo", str(self.repo), "--out", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        offered = set(self.value("--tools").split(","))
        self.assertTrue(offered, "no tool list was passed, so claude would offer its defaults")
        self.assertLessEqual(offered, READ_ONLY, f"a tool outside the read-only list: {offered - READ_ONLY}")
        self.assertFalse(offered & WRITERS)
        self.assertEqual(set(self.value("--allowedTools").split(",")), offered)
        # Whatever the list leaves out is denied, not asked; no MCP server and no user settings are loaded.
        self.assertEqual(self.value("--permission-mode"), "dontAsk")
        for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, self.argv())
        for word in self.argv():
            self.assertNotIn("bypassPermissions", word)
            self.assertNotIn("dangerously", word)

    def test_the_deep_lens_reaches_claude_in_the_checkout_and_its_answer_lands_in_out(self):
        result = self.gate("--repo", str(self.repo), "--out", str(self.out), "--model", "opus", "--effort", "max")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pathlib.Path(self.log["cwd"].read_text().strip()).resolve(), self.repo.resolve())
        prompt = self.log["stdin"].read_text()
        self.assertIn("The document is a whole repository, `fixture`", prompt)
        self.assertIn("**Use at most 8 areas.**", prompt)
        self.assertIn("You are a hostile domain expert", prompt)
        self.assertEqual((self.value("--model"), self.value("--effort")), ("opus", "max"))
        self.assertEqual(self.out.read_text(), "### 1. A finding\nConfidence: LIKELY\n")
        self.assertEqual(list(self.repo.iterdir()), [], "the run wrote into the checkout")

    def test_a_claude_failure_is_its_exit_code(self):
        result = self.gate("--repo", str(self.repo), "--out", str(self.out), mode="fail")
        self.assertEqual(result.returncode, 2)
        self.assertIn("error: not logged in", result.stderr)

    def test_the_bound_kills_a_claude_that_never_answers_and_its_tree(self):
        started = time.monotonic()
        result = self.gate("--repo", str(self.repo), "--out", str(self.out), "--timeout", "1", mode="hang")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("deep: claude did not answer within 1 s", result.stderr)
        self.assertLess(time.monotonic() - started, 10)
        pid = int(self.log["grandchild"].read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail(f"the fake's grandchild {pid} survived the bound")

    def test_no_claude_is_refused_before_anything_runs(self):
        result = self.gate("--repo", str(self.repo), "--out", str(self.out), claude=self.tmp / "absent")
        self.assertEqual(result.returncode, 1)
        self.assertIn("adversarial-gate: claude CLI not found", result.stderr)
        self.assertFalse(self.out.exists())

    def test_a_model_that_could_be_read_as_a_flag_is_refused(self):
        for value in ("--dangerously-skip-permissions", "opus --tools Bash", "-x"):
            with self.subTest(model=value):
                result = self.gate("--repo", str(self.repo), "--out", str(self.out), "--model", value)
                self.assertEqual(result.returncode, 1)
                self.assertIn("adversarial-gate: deep: --model wants a model id or alias", result.stderr)
                self.assertFalse(self.log["argv"].exists())

    def test_an_effort_that_is_not_one_lowercase_word_is_refused(self):
        result = self.gate("--repo", str(self.repo), "--out", str(self.out), "--effort", "max --x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("adversarial-gate: deep: --effort wants one lowercase word", result.stderr)
        self.assertFalse(self.log["argv"].exists())


if __name__ == "__main__":
    unittest.main()
