"""There is one harness, and it is the pack's.

Criterion 23's `system` half: a grep of the suite for a second GitHub double,
or for a patch of `subprocess` around `launchctl`, `tailscale`, `curl` or
`caffeinate`, returns nothing in this repository. The harness lives in the
pack's `lib/sd_db/testing`. The check is a grep of the working tree rather
than a list of files kept by hand, so a second double added tomorrow fails it
without anyone remembering to update a list. Stdlib only, so it runs in the
`tests/ci-native.sh` preflight.
"""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The commands the stubs replace. A `subprocess` patch naming one of them is
#: a second harness by another spelling.
STUBBED = ("launchctl", "tailscale", "curl", "caffeinate")


#: This file quotes every pattern it searches for, so it matches itself. A
#: test that matches itself measures nothing.
SELF = "tests/test_one_double.py"


def grep(pattern, *paths):
    """Tracked and untracked files, so a double added but not yet staged fails too."""
    completed = subprocess.run(
        ["git", "grep", "-nIE", "--untracked", pattern, "--", *(paths or ["."]), f":(exclude){SELF}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(completed.stderr)
    return [line for line in completed.stdout.splitlines() if line]


class TheOnlyHarness(unittest.TestCase):
    def test_no_second_github_double(self):
        hits = grep(r"(mock|patch|fake|double|stub)[^\n]*(gh |github)|github[^\n]*(mock|patch|fake|double)")
        self.assertEqual(hits, [], "a second GitHub double outside the pack's harness")

    def test_no_subprocess_patch_around_a_stubbed_command(self):
        for command in STUBBED:
            hits = grep(rf"(patch|mock)[^\n]*subprocess[^\n]*{command}|{command}[^\n]*(patch|mock)[^\n]*subprocess")
            self.assertEqual(hits, [], f"a subprocess patch around {command}")


if __name__ == "__main__":
    unittest.main()
