"""Nothing names the retired `local-sd-db` folder (sd:3281).

The pack owns `sd_db` and `bin/sd-db`; `local-sd-db/` is deleted. A caller that
still names it fails only when it runs, often unattended at night. This greps
the tracked and untracked files outside `docs/work` (history) so a stale path
fails here, and lists each place that may still say the name, with its reason.
Stdlib and git only, so it runs in the `tests/ci-native.sh` preflight.
"""

import subprocess
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
    "local-deploy/tests/test_deploy.py": "proves deploy plans and restarts nothing for the deleted folder",
}


class TheRetiredFolder(unittest.TestCase):
    def test_the_folder_is_gone(self):
        self.assertFalse((REPO_ROOT / RETIRED).exists(), f"{RETIRED}/ exists again")

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


if __name__ == "__main__":
    unittest.main()
