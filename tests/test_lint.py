"""Lint (sd:6742): shellcheck warnings on tracked shell, ruff E9 and F on tracked Python.

Both read `git ls-files`, so an untracked clone (local-kev's `kev/`) and the
gitignored `.ci/pack` are never linted. A finding fails with its file, line
and code; fix it, or put a `# shellcheck disable=SCnnnn # <reason>` or
`# noqa: Fnnn` beside it when the tool is wrong.

shellcheck and ruff come from Homebrew; tests/check.sh puts them on the
preflight's PATH. Stdlib and git only otherwise, so it runs in the
`tests/ci-native.sh` preflight before the venv.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Vendored Milvus code: `milvus.sh install` refreshes it, nobody edits it,
#: so a disable comment in it would be lost on the next refresh.
SHELL_SKIP = {"local-milvus/standalone_embed.sh"}


def tracked(pattern):
    done = subprocess.run(["git", "ls-files", "-z", pattern], cwd=ROOT,
                          capture_output=True, text=True, check=True)
    return sorted(p for p in done.stdout.split("\0") if p)


def lint(tool, args, files):
    path = shutil.which(tool)
    if path is None:
        raise AssertionError(f"{tool} is not on PATH; install it: brew install {tool}")
    done = subprocess.run([path, *args, *files], cwd=ROOT, capture_output=True, text=True)
    return done.returncode, (done.stdout + done.stderr).strip()


class Lint(unittest.TestCase):
    def test_tracked_shell_has_no_shellcheck_warning(self):
        files = [f for f in tracked("*.sh") if f not in SHELL_SKIP]
        self.assertGreater(len(files), 50, "the enumeration found too few scripts")
        rc, out = lint("shellcheck", ["-S", "warning", "-f", "gcc"], files)
        self.assertEqual((rc, out), (0, ""), out)

    def test_tracked_python_has_no_ruff_error_or_pyflakes_finding(self):
        files = tracked("*.py")
        self.assertGreater(len(files), 50, "the enumeration found too few modules")
        rc, out = lint("ruff", ["check", "--no-cache", "--quiet", "--select", "E9,F",
                                "--output-format", "concise"], files)
        self.assertEqual((rc, out), (0, ""), out)

    def test_the_skipped_script_still_exists(self):
        for name in SHELL_SKIP:
            self.assertIn(name, tracked(name), f"{name} is gone; drop it from SHELL_SKIP")


if __name__ == "__main__":
    unittest.main()
