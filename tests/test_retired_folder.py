"""Retired folders are gone, and nothing names them (sd:3281, sd:3371, pack sd:5987).

The pack owns `sd_db` and `bin/sd-db`, and since pack sd:5987 the workflow
dashboard too, at `$SD_PACK_ROOT/dashboard/`. The folders that held them here
are deleted; `machine-setup.sh doctor` fails any config that still names one.
A caller that names a deleted folder fails only when it runs, often unattended
at night. This fails each folder and greps the tracked and untracked files
outside `docs/work` (history), so a stale path fails here, and lists each place
that may still say a name, with its reason. Stdlib and git only, so it runs in
the `tests/ci-native.sh` preflight.
"""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Each retired folder, its name built in two parts so this file does not
#: match itself: where its callers point now, and every file that may still
#: say the name, with why.
RETIRED = {
    "local-" + "sd-db": ("$SD_PACK_ROOT/bin/sd-db", {
        "CLAUDE.md": "the rule that points at this test",
        "tests/ci-native.sh": "the comment above the preflight line that runs this test",
        "local-machine-setup/machine-setup.sh": "the doctor check that finds config still naming the folder",
        "local-machine-setup/README.md": "documents that doctor check",
        "local-machine-setup/tests/test_backbone.py": "plants the retired name to prove the doctor finds it",
        "local-deploy/tests/test_deploy.py": "proves deploy plans and restarts nothing across a range that deleted the folder",
    }),
    "local-" + "project-dashboard": ("$SD_PACK_ROOT/dashboard/dashboard.sh", {
        "CLAUDE.md": "the rule that names where the dashboard went",
        "local-machine-setup/machine-setup.sh": "the doctor check that finds config still naming the folder",
        "local-machine-setup/tests/test_backbone.py": "plants the retired name to prove the doctor finds it",
        "local-deploy/deploy.sh": "plans a dashboard restart across a range that changes the folder, as its deletion does",
        "local-deploy/README.md": "documents that plan line",
        "local-deploy/tests/test_deploy.py": "changes files in the folder to prove that plan line",
    }),
}


class TheRetiredFolders(unittest.TestCase):
    def test_each_folder_is_gone(self):
        for retired in RETIRED:
            with self.subTest(folder=retired):
                self.assertFalse((REPO_ROOT / retired).exists(), f"{retired}/ must not exist")
                completed = subprocess.run(
                    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", retired],
                    cwd=REPO_ROOT, capture_output=True, text=True, check=True,
                )
                self.assertEqual(completed.stdout.split(), [])

    def test_only_listed_files_name_each(self):
        for retired, (target, allowed) in RETIRED.items():
            with self.subTest(folder=retired):
                completed = subprocess.run(
                    ["git", "grep", "-lIF", "--untracked", retired, "--", ".", ":(exclude)docs/work"],
                    cwd=REPO_ROOT, capture_output=True, text=True,
                )
                if completed.returncode not in (0, 1):
                    raise AssertionError(completed.stderr)
                named = {line for line in completed.stdout.splitlines() if line}
                named.discard("tests/test_retired_folder.py")
                self.assertEqual(sorted(named - set(allowed)), [],
                                 f"these files name the deleted folder: point them at {target}")
                self.assertEqual(sorted(set(allowed) - named), [],
                                 "these allowed entries no longer name it: drop them from the list")


if __name__ == "__main__":
    unittest.main()
