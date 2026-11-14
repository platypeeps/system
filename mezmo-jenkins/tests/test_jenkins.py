"""jenkins.sh log|wait reads a PR build's console from Jenkins (sd:8796).

A fake curl serves canned answers by URL path and logs every call; a fake
`sdm status` lists the Jenkins row. No test reaches a network.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"

FAKE_CURL = r'''#!PYTHON
import json, os, sys, urllib.parse
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write(" ".join(args) + "\n")
url = next(a for a in args if a.startswith("http"))
parts = urllib.parse.urlsplit(url)
if parts.hostname in os.environ.get("FAKE_UNRESOLVED", "").split():
    sys.stderr.write("curl: (6) Could not resolve host: %s\n" % parts.hostname)
    sys.exit(6)
if parts.hostname in ("127.0.0.1", "jenkins.example.test") and \
        parts.netloc != os.environ.get("FAKE_SERVES", ""):
    sys.stderr.write("curl: (7) Failed to connect\n")
    sys.exit(7)
state = os.environ["FAKE_STATE"]
answers = json.load(open(os.environ["FAKE_ANSWERS"]))
seen = json.load(open(state)) if os.path.exists(state) else {}
queue = answers.get(parts.path)
if queue is None:
    sys.stderr.write("curl: (22) The requested URL returned error: 404\n")
    sys.exit(22)
n = seen.get(parts.path, 0)
seen[parts.path] = n + 1
json.dump(seen, open(state, "w"))
body = queue[min(n, len(queue) - 1)]
out = args[args.index("-o") + 1] if "-o" in args else None
if out:
    open(out, "w").write(body)
else:
    sys.stdout.write(body)
'''

SDM_STATUS = """\
     WEBSITE      STATUS               ADDRESS             TYPE        TAGS   URL
     Jenkins      connected (auto)     127.0.0.1:10999     httpNoAuth  a=b    http://jenkins.example.test
     Other        connected (auto)     127.0.0.1:10001     httpNoAuth  a=b    http://other.example.test/
"""

JOB = "/job/example/job/widget/job/PR-7"


class JenkinsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jenkins-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "mezmo-jenkins").mkdir()
        shutil.copy(FOLDER / "jenkins.sh", self.tmp / "mezmo-jenkins")
        shutil.copytree(LIB, self.tmp / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text(FAKE_CURL.replace("PYTHON", sys.executable))
        curl.chmod(0o755)
        self.sdm = self.bin / "sdm"
        self.sdm.write_text("#!/bin/sh\n[ \"$1\" = status ] || exit 2\ncat <<'EOF'\n" + SDM_STATUS + "EOF\n")
        self.sdm.chmod(0o755)
        self.config = self.tmp / "config" / "mezmo-jenkins"
        self.config.mkdir(parents=True)
        (self.config / ".env").write_text(
            "JENKINS_URL=http://jenkins.example.test\n"
            "JENKINS_JOB_PATH='job/{owner}/job/{repo}/job/PR-{pr}'\n")
        self.calls = self.tmp / "calls"
        self.answers = {"/api/json": ['{"mode":"NORMAL"}']}
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "SYSTEM_TOOLS_CONFIG": str(self.tmp / "config"),
            "JENKINS_SDM": str(self.sdm),
            "JENKINS_POLL_SECS": "0",
            "FAKE_CALLS": str(self.calls),
            "FAKE_STATE": str(self.tmp / "state.json"),
            "FAKE_ANSWERS": str(self.tmp / "answers.json"),
            "FAKE_SERVES": "jenkins.example.test",
        }

    def run_jenkins(self, *args, **extra):
        (self.tmp / "answers.json").write_text(json.dumps(self.answers))
        return subprocess.run(["sh", str(self.tmp / "mezmo-jenkins" / "jenkins.sh"), *args],
                              capture_output=True, text=True, env={**self.env, **extra}, timeout=60)

    def urls(self):
        return [line.split()[-1] for line in self.calls.read_text().splitlines()]

    def test_log_prints_the_newest_builds_console(self):
        self.answers[JOB + "/lastBuild/consoleText"] = ["step one\nFinished: SUCCESS\n"]
        run = self.run_jenkins("log", "example/widget#7")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual("step one\nFinished: SUCCESS\n", run.stdout)
        self.assertIn("http://jenkins.example.test" + JOB + "/lastBuild/consoleText", self.urls())

    def test_log_takes_a_build_number(self):
        self.answers[JOB + "/3/consoleText"] = ["third\n"]
        run = self.run_jenkins("log", "example/widget#7", "3")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual("third\n", run.stdout)

    def test_unresolved_name_falls_back_to_the_sdm_port(self):
        self.answers[JOB + "/lastBuild/consoleText"] = ["via port\n"]
        run = self.run_jenkins("log", "example/widget#7",
                               FAKE_UNRESOLVED="jenkins.example.test", FAKE_SERVES="127.0.0.1:10999")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual("via port\n", run.stdout)
        self.assertIn("http://127.0.0.1:10999" + JOB + "/lastBuild/consoleText", self.urls())

    def test_neither_name_nor_port_answers(self):
        run = self.run_jenkins("log", "example/widget#7",
                               FAKE_UNRESOLVED="jenkins.example.test", FAKE_SERVES="nothing")
        self.assertEqual(1, run.returncode)
        self.assertIn("jenkins.example.test", run.stderr)
        self.assertIn("sdm", run.stderr)

    def test_every_curl_call_is_bounded(self):
        self.answers[JOB + "/lastBuild/consoleText"] = ["x\n"]
        self.run_jenkins("log", "example/widget#7",
                         FAKE_UNRESOLVED="jenkins.example.test", FAKE_SERVES="127.0.0.1:10999")
        lines = self.calls.read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("--max-time", line.split(), line)

    def test_wait_prints_the_result_and_failing_lines(self):
        self.answers[JOB + "/lastBuild/api/json"] = [
            '{"number": 4, "building": true, "result": null}',
            '{"number": 4, "building": false, "result": "FAILURE"}']
        self.answers[JOB + "/4/consoleText"] = [
            "checkout ok\nrazee FAIL deploy-widget\nError: boom\nall good here\n"]
        run = self.run_jenkins("wait", "example/widget#7")
        self.assertEqual(1, run.returncode, run.stderr)
        self.assertIn("build 4: FAILURE", run.stdout)
        self.assertIn("razee FAIL deploy-widget", run.stdout)
        self.assertIn("Error: boom", run.stdout)
        self.assertNotIn("checkout ok", run.stdout)

    def test_wait_waits_for_a_build_that_is_not_there_yet(self):
        # The first poll 404s: no build yet. The poll's sleep swaps in the
        # answers that have the build.
        later = dict(self.answers)
        later[JOB + "/lastBuild/api/json"] = ['{"number": 1, "building": false, "result": "SUCCESS"}']
        later[JOB + "/1/consoleText"] = ["Finished: SUCCESS\n"]
        (self.tmp / "later.json").write_text(json.dumps(later))
        sleep = self.bin / "sleep"
        sleep.write_text(f"#!/bin/sh\ncp '{self.tmp / 'later.json'}' '{self.tmp / 'answers.json'}'\n")
        sleep.chmod(0o755)
        run = self.run_jenkins("wait", "example/widget#7", JENKINS_POLL_SECS="1")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("build 1: SUCCESS", run.stdout)

    def test_wait_succeeds_on_success(self):
        self.answers[JOB + "/lastBuild/api/json"] = ['{"number": 2, "building": false, "result": "SUCCESS"}']
        self.answers[JOB + "/2/consoleText"] = ["Finished: SUCCESS\n"]
        run = self.run_jenkins("wait", "example/widget#7")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("build 2: SUCCESS", run.stdout)

    def test_wait_times_out(self):
        self.answers[JOB + "/lastBuild/api/json"] = ['{"number": 5, "building": true, "result": null}']
        run = self.run_jenkins("wait", "example/widget#7", "--timeout", "0")
        self.assertEqual(1, run.returncode)
        self.assertIn("timed out", run.stderr)

    def test_missing_config_names_both_remedies(self):
        (self.config / ".env").unlink()
        run = self.run_jenkins("log", "example/widget#7")
        self.assertEqual(1, run.returncode)
        self.assertIn("JENKINS_URL", run.stderr)
        self.assertIn(".env.example", run.stderr)

    def test_a_bad_pr_spec_is_refused(self):
        run = self.run_jenkins("log", "widget-7")
        self.assertEqual(1, run.returncode)
        self.assertIn("owner/repo#N", run.stderr)


if __name__ == "__main__":
    unittest.main()
