"""Every folder's entrypoint is executable in the git index (sd:2648).

Convention 1 gives each runnable folder one entrypoint, and `./<name>.sh`
only runs when git tracks it as mode 100755. An entrypoint once tracked as
100644 exited 126 when run directly and nothing noticed: `sh <file>`
works either way, and that is how every suite calls it.

The entrypoints are read from `git ls-files -s`, not listed here, so a new
folder is checked the day it is tracked. A folder's entrypoint is the one
local-health-check probes: `<stem>.sh`, where the stem is the folder name
minus `local-` or `mezmo-`, or else the folder's only root `*.sh`. A folder
with neither (`lib/`, `tests/`) has no entrypoint and is skipped.

Stdlib and git only. This runs in the `tests/ci-native.sh` preflight as
`python3 tests/test_entrypoint_mode.py` from the repository root, and the
preflight's unwired-suite guard fails when `tests/ci-native.sh` stops naming it.
"""

import subprocess
import sys
import unittest
from pathlib import Path

from test_product_name import PREFIX as VENDOR

ROOT = Path(__file__).resolve().parent.parent


def index(root=ROOT):
    """`git ls-files -s` as {path: mode}."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=root, capture_output=True, text=True, check=True,
    )
    entries = {}
    for record in filter(None, out.stdout.split("\0")):
        meta, path = record.split("\t", 1)
        entries[path] = meta.split()[0]
    return entries


def entrypoints(entries):
    """{entrypoint path: mode} for each top-level folder that has one."""
    roots = {}
    for path, mode in entries.items():
        parts = path.split("/")
        if len(parts) == 2 and parts[1].endswith(".sh"):
            roots.setdefault(parts[0], {})[parts[1]] = mode
    found = {}
    for folder, scripts in roots.items():
        stem = folder.removeprefix("local-").removeprefix(VENDOR)
        name = f"{stem}.sh" if f"{stem}.sh" in scripts else None
        if name is None and len(scripts) == 1:
            name = next(iter(scripts))
        if name is not None:
            found[f"{folder}/{name}"] = scripts[name]
    return found


class TheTree(unittest.TestCase):
    def test_every_entrypoint_is_tracked_executable(self):
        found = entrypoints(index())
        self.assertIn("local-sd-db/sd-db.sh", found, "the enumeration found no entrypoints")
        bad = sorted(p for p, mode in found.items() if mode != "100755")
        self.assertEqual([], bad, "tracked without the executable bit; run "
                         "`git update-index --chmod=+x <path>` for each")

    def test_the_preflight_runs_this_file(self):
        lines = (ROOT / "tests/ci-native.sh").read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            "python3 tests/test_entrypoint_mode.py" in lines,
            "the tests/ci-native.sh preflight no longer runs this file",
        )


class TheRule(unittest.TestCase):
    def test_the_stem_wins_and_a_lone_script_stands_in(self):
        found = entrypoints({
            "local-x/x.sh": "100755", "local-x/helper.sh": "100644",
            "mezmo-pipeline/pipeline.sh": "100644",
            "plain/plain.sh": "100755",
            "local-runner/runner.sh": "100644",
            "lib/a.sh": "100644", "lib/b.sh": "100644",
            "local-x/tests/deep.sh": "100644", "top.sh": "100644",
        })
        self.assertEqual({
            "local-x/x.sh": "100755", "mezmo-pipeline/pipeline.sh": "100644",
            "plain/plain.sh": "100755", "local-runner/runner.sh": "100644",
        }, found)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
