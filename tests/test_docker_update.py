"""Every docker `update` removes images by ID the way Docker 29 allows (sd:3549).

Docker 29 prints `IMAGE  ID  DISK USAGE ...` with `repo:tag` in one column, so
`awk '{print $3}'` on `docker images` yields a size and `grep "^name "` never
matches. `docker images -aq --filter "reference=<repo>"` prints IDs only and
does not depend on column order. The check reads the tracked entrypoints, so a
wrapper added tomorrow is held to the same form. Stdlib and git only, so it
runs in the `tests/ci-native.sh` preflight.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: This file quotes the broken form.
SELF = "tests/test_docker_update.py"

GOOD = re.compile(r'docker images -aq --filter "reference=[^" ]+"')


def grep(pattern, *paths):
    done = subprocess.run(
        ["git", "grep", "-nIE", "--untracked", pattern, "--", *paths, f":(exclude){SELF}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if done.returncode not in (0, 1):
        raise AssertionError(done.stderr)
    return [line for line in done.stdout.splitlines() if line]


class DockerImagesByID(unittest.TestCase):
    def test_every_images_call_in_a_script_filters_by_reference(self):
        calls = grep(r"docker images", "*.sh")
        self.assertGreaterEqual(len(calls), 8, "the enumeration found too few wrappers")
        bad = [c for c in calls if not GOOD.search(c)]
        self.assertEqual(bad, [], "docker images must be `-aq --filter \"reference=<repo>\"`")

    def test_no_script_reads_a_docker_images_column(self):
        self.assertEqual(grep(r"docker images.*(awk|cut|grep)", "*.sh"), [])

    def test_the_convention_text_does_not_prescribe_the_column(self):
        self.assertEqual(grep(r"awk '\{print \$3\}'", "CLAUDE.md", "README.md"), [])


if __name__ == "__main__":
    unittest.main()
