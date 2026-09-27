"""There is one harness in this repository, and it is this one.

Criterion 23's `system` half: a grep of the suite for a second GitHub double,
or for a patch of `subprocess` around `launchctl`, `tailscale`, `curl` or
`caffeinate`, returns nothing outside this package. The check is a grep of
the working tree rather than a list of files kept by hand, so a second double
added tomorrow fails it without anyone remembering to update a list.
"""

import subprocess
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent

#: Where the harness itself lives. Everything under it is allowed to name
#: these things; that is what it is for.
ALLOWED = "local-sd-db/"

#: The commands the stubs replace. A `subprocess` patch naming one of them is
#: a second harness by another spelling.
STUBBED = ("launchctl", "tailscale", "curl", "caffeinate")


#: This file quotes every pattern it searches for, so it matches itself. A
#: test that matches itself measures nothing.
SELF = "local-sd-db/tests/test_one_double.py"


def grep(pattern, *paths):
    """Tracked and untracked files, so a double added but not yet staged fails too."""
    completed = subprocess.run(
        ["git", "grep", "-nIE", "--untracked", pattern, "--", *(paths or ["."]), f":(exclude){SELF}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(completed.stderr)
    return [line for line in completed.stdout.splitlines() if line]


def outside_the_harness(lines):
    return [line for line in lines if not line.startswith(ALLOWED)]


class TheOnlyHarness(unittest.TestCase):
    def test_no_second_github_double(self):
        hits = outside_the_harness(
            grep(r"(mock|patch|fake|double|stub)[^\n]*(gh |github)|github[^\n]*(mock|patch|fake|double)")
        )
        self.assertEqual(hits, [], "a second GitHub double outside the harness")

    def test_no_subprocess_patch_around_a_stubbed_command(self):
        for command in STUBBED:
            hits = outside_the_harness(
                grep(rf"(patch|mock)[^\n]*subprocess[^\n]*{command}|{command}[^\n]*(patch|mock)[^\n]*subprocess")
            )
            self.assertEqual(hits, [], f"a subprocess patch around {command}")

    def test_the_harness_is_where_it_says_it_is(self):
        """The grep is only meaningful if it can see the harness at all."""
        hits = grep(r"class GitHubDouble")
        self.assertEqual(len(hits), 1, hits)
        self.assertTrue(hits[0].startswith(ALLOWED), hits[0])


if __name__ == "__main__":
    unittest.main()
