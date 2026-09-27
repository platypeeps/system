"""The entrypoint's conventions, and what `status` can and cannot claim.

Python rather than shell for the reason `local-repo-sync/README.md:90` gives:
the CI wrapper asserts a `Ran N tests` summary and fails on skips, and a shell
harness produces neither.

`status` is the whole surface this folder ships today, and the thing worth
pinning about it is that its three exit codes mean three different things to
`local-health-check` -- 3 is a machine that was never set up, 1 is one that
was and is broken, and conflating them is how a nightly finding nobody can act
on gets raised forever.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "sd-plan.sh"

#: A runner that answers `status` the way the real one does. The script reads
#: two fields out of that JSON and nothing else, so a double can be this small.
RUNNER = """\
#!/bin/sh
[ "$1" = status ] || exit 1
cat <<'JSON'
{"healthy": %s, "storage": {"dispatch_allowed": %s, "ok": true}}
JSON
"""


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        # The conf has to sit beside the script, where a developer's own
        # `repos.<profile>.conf` lives too. A profile named after this case's
        # temporary folder is one nobody else holds, so the cleanup below can
        # only ever remove a file this case wrote (sd:1235).
        self.profile = f"fixture-{self.home.name}"
        self.conf = FOLDER / f"repos.{self.profile}.conf"
        self.assertFalse(self.conf.exists(), f"{self.conf} predates the case")
        self.addCleanup(lambda: self.conf.unlink(missing_ok=True))

    def runner(self, *, healthy=True, dispatching=True):
        path = self.home / "runner.sh"
        path.write_text(
            RUNNER % ("true" if healthy else "false",
                      "true" if dispatching else "false"),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def participants(self, text):
        self.conf.write_text(text, encoding="utf-8")

    def plan(self, *args, expect=0, runner=None):
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        # The profile selects the conf file; pinning it keeps the suite off
        # whatever this machine happens to be.
        environment["SD_PLAN_PROFILE"] = self.profile
        environment["MACHINE_SETUP_STATE"] = str(self.home / "absent")
        if runner is not None:
            environment["SD_PLAN_RUNNER"] = str(runner)
        done = subprocess.run(
            ["/bin/sh", str(ENTRYPOINT), *args],
            capture_output=True, text=True, input="", env=environment,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done


class TheConventions(PlanCase):
    def test_it_is_posix_sh_and_resolves_its_own_folder(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("set -e", text)
        self.assertIn('DIR="$(cd "$(dirname "$0")" && pwd)"', text)

    def test_it_is_named_after_the_folder_without_the_prefix(self):
        self.assertEqual(ENTRYPOINT.name, FOLDER.name.replace("local-", "") + ".sh")

    def test_no_argument_prints_usage_to_stderr_and_exits_one(self):
        done = self.plan(expect=1)
        self.assertEqual(done.stdout, "")
        self.assertIn("Usage: sd-plan.sh", done.stderr)

    def test_an_unknown_verb_is_refused_rather_than_ignored(self):
        done = self.plan("plan-everything", expect=1)
        self.assertIn("Usage: sd-plan.sh", done.stderr)

    def test_help_exits_zero_and_names_every_verb_the_script_dispatches(self):
        """The verbs come from the `case`, not from a list written here.

        `local-sd-db/tests/test_cli.py` had that list as a tuple and it named
        ten verbs, agreeing with itself, while an eleventh was dispatched and
        documented. Reading the dispatcher is the only version of this check
        that can see a verb nobody remembered to document.
        """
        done = self.plan("help")
        body = ENTRYPOINT.read_text(encoding="utf-8").partition("\ncase ")[2]
        labels = re.findall(r"^    ([a-z|*-]+)\)$", body, re.MULTILINE)
        verbs = sorted(
            verb
            for label in labels
            for verb in label.split("|")
            if verb not in ("*", "-h", "--help")
        )
        self.assertTrue(verbs, "read no verbs out of the dispatcher")
        entries = set(re.findall(r"^  ([a-z]+)\b", done.stderr, re.MULTILINE))
        for verb in verbs:
            self.assertIn(verb, entries, f"{verb} is dispatched and undocumented")


class WhatStatusMeans(PlanCase):
    def test_no_participation_list_is_nothing_to_check_and_not_a_fault(self):
        # 3, not 1. A machine that was never set up for this must stay silent
        # in local-health-check rather than raise a finding nobody can act on.
        done = self.plan("status", expect=3, runner=self.runner())
        self.assertIn("SKIP", done.stdout)
        self.assertIn(self.conf.name, done.stdout)

    def test_a_list_naming_nothing_is_also_nothing_to_check(self):
        self.participants("# every line here is a comment\n\n   \n")
        done = self.plan("status", expect=3, runner=self.runner())
        self.assertIn("names no repository", done.stdout)

    def test_participants_and_a_dispatching_runner_is_healthy(self):
        self.participants(
            "# a comment\n"
            "/Users/nobody/repos/one\n"
            "\n"
            "   /Users/nobody/repos/two   # trailing comment\n"
        )
        done = self.plan("status", runner=self.runner())
        self.assertIn("2 repository(ies) participate", done.stdout)
        self.assertIn("runner dispatching", done.stdout)

    def test_participants_and_a_runner_that_would_not_dispatch_is_broken(self):
        # The failure this distinguishes: `nightly` enqueues into a queue
        # nobody drains, exits 0, and reports nothing, every night.
        self.participants("/Users/nobody/repos/one\n")
        done = self.plan("status", expect=1, runner=self.runner(dispatching=False))
        self.assertIn("FAIL", done.stdout)
        self.assertIn("not dispatching", done.stdout)

    def test_an_unhealthy_runner_is_broken_even_when_dispatch_is_allowed(self):
        self.participants("/Users/nobody/repos/one\n")
        self.plan("status", expect=1, runner=self.runner(healthy=False, dispatching=True))

    def test_a_runner_whose_agent_is_not_loaded_is_broken_and_says_so(self):
        """sd:1387. The runner's 3 stays this tool's 1, and line 1 names it.

        `sd-runner status` exits 3 when launchd does not hold its agent. That
        is sd-runner's "nothing to check", but it is also what a provisioned
        agent that was booted out looks like, and `nightly` refuses to
        enqueue either way. A participation list says this machine plans, so
        passing 3 through would silence the check while the night fails. The
        sweep quotes line 1, so the cause goes there rather than the generic
        "not dispatching".
        """
        path = self.home / "not-loaded.sh"
        path.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = status ] || exit 1\n"
            "echo '{\"ok\": false, \"reason\": \"no database\"}'\n"
            "exit 3\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        self.participants("/Users/nobody/repos/one\n")
        done = self.plan("status", expect=1, runner=path)
        first = done.stdout.splitlines()[0]
        self.assertTrue(first.startswith("local-sd-plan: FAIL"), first)
        self.assertIn("agent is not loaded", first)

    def test_a_runner_that_fails_its_own_status_is_broken(self):
        # PIN. The runner's 1 is this tool's 1, with the generic cause.
        path = self.home / "stale.sh"
        path.write_text("#!/bin/sh\necho '{\"ok\": false}'\nexit 1\n", encoding="utf-8")
        path.chmod(0o755)
        self.participants("/Users/nobody/repos/one\n")
        done = self.plan("status", expect=1, runner=path)
        self.assertIn("FAIL", done.stdout)

    def test_a_runner_that_is_not_installed_is_broken_not_absent(self):
        # Deliberately 1 and not 3: the list says this machine plans. A
        # missing runner is then a broken machine, not an unconfigured one.
        self.participants("/Users/nobody/repos/one\n")
        self.plan("status", expect=1, runner=self.home / "not-installed.sh")

    def test_a_runner_that_answers_with_something_that_is_not_json(self):
        path = self.home / "babbling.sh"
        path.write_text("#!/bin/sh\necho not json at all\n", encoding="utf-8")
        path.chmod(0o755)
        self.participants("/Users/nobody/repos/one\n")
        self.plan("status", expect=1, runner=path)


class TheDevelopersOwnList(unittest.TestCase):
    def test_a_case_leaves_a_conf_it_did_not_write_alone(self):
        """sd:1235. Every case once wrote and then unlinked
        `repos.fixture.conf` beside the script, whoever's it was.

        One is staged here if none is there, and a whole case that writes a
        participation list runs against it. The file must come back byte for
        byte; a staged one is removed afterwards, a developer's is not.
        """
        mine = FOLDER / "repos.fixture.conf"
        if not mine.exists():
            mine.write_text("/Users/somebody/repos/theirs\n", encoding="utf-8")
            self.addCleanup(mine.unlink, missing_ok=True)
        before = mine.read_bytes()
        result = unittest.TestResult()
        WhatStatusMeans("test_participants_and_a_dispatching_runner_is_healthy").run(result)
        self.assertEqual([], [text for _, text in result.errors + result.failures])
        self.assertTrue(mine.exists(), "the case deleted a conf it did not write")
        self.assertEqual(before, mine.read_bytes(), "the case overwrote a conf it did not write")


class TheExampleList(unittest.TestCase):
    def test_the_shipped_example_names_no_live_repository(self):
        """It ships commented out, and that is the whole point of it.

        An example that participates on copy opts a repository into unattended
        branch-writing by being copied, which is the one thing this file must
        not do.
        """
        example = FOLDER / "repos.personal.conf.example"
        self.assertTrue(example.is_file())
        live = [
            line for line in example.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(live, [])

    def test_no_participation_list_is_committed(self):
        """`repos.<profile>.conf` is local, gitignored configuration.

        It names the operator's repositories, so it never ships. This fails
        the moment someone commits one. Outside a git checkout nothing is
        committed, so the folder itself must hold none.
        """
        listed = subprocess.run(["git", "ls-files", "--", "repos.*.conf"], cwd=FOLDER,
                                capture_output=True, text=True)
        if listed.returncode == 0:
            self.assertEqual(listed.stdout.split(), [])
        else:
            self.assertEqual(sorted(p.name for p in FOLDER.glob("repos.*.conf")), [])


if __name__ == "__main__":
    unittest.main()
