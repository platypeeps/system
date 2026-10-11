"""Nothing but a forwarder is left of the retired `local-sd-db` folder (sd:3281).

The pack owns `sd_db` and `bin/sd-db`. `local-sd-db/sd-db.sh` stays as a
forwarder that warns and execs the pack's `bin/sd-db`, so an installed plist or
cron job that still names it keeps working until `machine-setup.sh doctor`
shows none does. A caller that names the folder fails only when it runs, often
unattended at night. This greps the tracked and untracked files outside
`docs/work` (history) so a stale path fails here, and lists each place that may
still say the name, with its reason. Stdlib and git only, so it runs in the
`tests/ci-native.sh` preflight.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The retired folder's name, built in two parts so this file does not match itself.
RETIRED = "local-" + "sd-db"

#: Every file that may still say the name, and why.
ALLOWED = {
    "CLAUDE.md": "the rule that points at this test",
    "tests/ci-native.sh": "the comment above the preflight line that runs this test",
    "local-machine-setup/machine-setup.sh": "the doctor check that finds config still naming the folder",
    "local-machine-setup/README.md": "documents that doctor check",
    "local-machine-setup/tests/test_backbone.py": "plants the retired name to prove the doctor finds it",
    "local-deploy/tests/test_deploy.py": "proves deploy plans and restarts nothing for the retired folder",
    "local-sd-db/sd-db.sh": "the forwarder: its warning names itself",
}

FORWARDER = REPO_ROOT / RETIRED / "sd-db.sh"

#: A stand-in for the pack's `bin/sd-db`: argv one per line on stdout, a
#: marker on stderr, and the exit code the test asks for.
STUB = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg"; done
echo stub-stderr >&2
exit "${STUB_EXIT:-0}"
"""


class TheRetiredFolder(unittest.TestCase):
    def test_only_the_forwarder_is_left(self):
        completed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", RETIRED],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        self.assertEqual(completed.stdout.split(), [f"{RETIRED}/sd-db.sh"])

    def test_only_listed_files_name_it(self):
        completed = subprocess.run(
            ["git", "grep", "-lIF", "--untracked", RETIRED, "--", ".", ":(exclude)docs/work"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if completed.returncode not in (0, 1):
            raise AssertionError(completed.stderr)
        named = {line for line in completed.stdout.splitlines() if line}
        named.discard("tests/test_retired_folder.py")
        self.assertEqual(sorted(named - set(ALLOWED)), [],
                         "these files name the deleted folder: point them at $SD_PACK_ROOT/bin/sd-db")
        self.assertEqual(sorted(set(ALLOWED) - named), [],
                         "these ALLOWED entries no longer name it: drop them from the list")


class TheForwarder(unittest.TestCase):
    def run_forwarder(self, *args, home=None, pack=None, exit_code=None):
        """Runs the forwarder against a stub pack; returns the completed process."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pack"
            if pack is None:
                pack = root
            stub = (root if home is None else Path(tmp) / "repos/platypeeps/sd-ai-command-pack") / "bin/sd-db"
            stub.parent.mkdir(parents=True)
            stub.write_text(STUB)
            stub.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k not in ("SD_PACK_ROOT", "STUB_EXIT")}
            env["HOME"] = tmp if home is not None else str(Path(tmp) / "nohome")
            if home is None:
                env["SD_PACK_ROOT"] = str(pack)
            if exit_code is not None:
                env["STUB_EXIT"] = str(exit_code)
            return subprocess.run(["sh", str(FORWARDER), *args], env=env, capture_output=True, text=True)

    def test_it_is_executable(self):
        self.assertTrue(os.access(FORWARDER, os.X_OK))

    def test_arguments_and_exit_code_pass_through(self):
        done = self.run_forwarder("repo", "list", "two words", exit_code=7)
        self.assertEqual(done.stdout, "repo\nlist\ntwo words\n")
        self.assertEqual(done.returncode, 7)

    def test_the_warning_is_on_stderr_only_and_names_the_pack_and_the_doctor(self):
        done = self.run_forwarder("status")
        self.assertNotIn(RETIRED, done.stdout)
        self.assertIn("stub-stderr", done.stderr)
        self.assertIn(f"{RETIRED}/sd-db.sh is retired", done.stderr)
        self.assertIn("bin/sd-db", done.stderr)
        self.assertIn("machine-setup.sh doctor", done.stderr)

    def test_the_default_pack_is_under_home(self):
        done = self.run_forwarder("status", home=True)
        self.assertEqual((done.stdout, done.returncode), ("status\n", 0), done.stderr)

    def test_a_missing_pack_fails_and_still_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k != "SD_PACK_ROOT"}
            env["SD_PACK_ROOT"] = str(Path(tmp) / "absent")
            done = subprocess.run(["sh", str(FORWARDER), "status"], env=env, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f"{RETIRED}/sd-db.sh is retired", done.stderr)


if __name__ == "__main__":
    unittest.main()
