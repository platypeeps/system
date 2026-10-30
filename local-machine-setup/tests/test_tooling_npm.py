"""The tooling stage installs the global npm CLIs a `.npm` profile file lists.

`common.npm` and `<profile>.npm` list packages, one per line, as every other
kind does; a line may carry `pkg@version`, which passes through. The npm is
the Homebrew one of the first node formula in the brew manifest: every
Homebrew node keeps its globals under `$(brew --prefix)`, on PATH in every
shell, where an fnm node keeps them per version (sd:4739).

A package counts as present when `npm ls -g --depth=0 --parseable <pkg>`
prints a path. Not by exit code: npm 10 exits 0 on a miss, and npm 11 exits 1
for a package that is present in a tree with problems elsewhere.

The script runs from a copy of this folder. `brew --prefix` names a folder in
the case's temporary root. Its keg's npm is an `env node` script, as
Homebrew's is, and the keg's node stub logs the arguments it gets.
"""

import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

PRESENT = "@example/present-cli"
ABSENT = "@example/absent-cli"

# Homebrew's npm is a script that `env` hands to the first node on PATH.
NPM_SCRIPT = "#!/usr/bin/env node\n// npm-cli.js\n"

# The keg's node, running that script: `ls` prints a path for each package in
# $NPM_HAVE and exits $NPM_LS_RC; every call is logged.
NODE_STUB = r"""#!/bin/sh
shift
echo "$*" >> "$NPM_LOG"
case "$1" in
  ls)
    for a in "$@"; do last="$a"; done
    for h in $NPM_HAVE; do
      [ "$h" = "$last" ] && echo "$NPM_PREFIX/lib/node_modules/$h"
    done
    exit "${NPM_LS_RC:-0}" ;;
esac
exit 0
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ToolingNpmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        (self.profiles / "common.brew").write_text("git\nnode\n")
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.prefix = base / "brew-prefix"
        self.npm = self.prefix / "opt/node/bin/npm"
        write_exec(self.npm, NPM_SCRIPT)
        write_exec(self.npm.parent / "node", NODE_STUB)
        self.npm_log = base / "npm.log"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", f'#!/bin/sh\n[ "$*" = --prefix ] && echo "{self.prefix}"\nexit 0\n')
        write_exec(self.stubs / "sudo", "#!/bin/sh\nexit 1\n")
        fixture_config.seal(self, self.stubs)
        self.have = PRESENT
        self.ls_rc = 0

    def run_verb(self, *args):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "NPM_LOG": str(self.npm_log),
            "NPM_HAVE": self.have,
            "NPM_LS_RC": str(self.ls_rc),
            "NPM_PREFIX": str(self.prefix),
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args],
                              env=env, capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=60)

    def tooling(self, *flags):
        result = self.run_verb("update", "tooling", *flags)
        return result.stdout + result.stderr

    def installs(self):
        calls = self.npm_log.read_text().splitlines() if self.npm_log.exists() else []
        return [c for c in calls if c.startswith("install")]

    def test_a_dry_run_names_the_missing_package_and_installs_nothing(self):
        (self.profiles / "common.npm").write_text(f"# CLIs\n{PRESENT}\n")
        (self.profiles / "personal.npm").write_text(f"{ABSENT}  # a comment\n")

        out = self.tooling()

        self.assertIn(f"  ok      npm {PRESENT}", out)
        self.assertIn(f"  MISSING npm {ABSENT}", out)
        self.assertIn(f"  [dry-run] npm install -g {ABSENT}", out)
        self.assertEqual(self.installs(), [])

    def test_apply_installs_only_the_missing_package_and_passes_a_version_through(self):
        (self.profiles / "personal.npm").write_text(f"{PRESENT}\n{ABSENT}@1.2.3\n")

        out = self.tooling("--apply")

        self.assertEqual(self.installs(), [f"install -g {ABSENT}@1.2.3"], out)

    def test_the_example_manifest_names_the_unversioned_node(self):
        """A versioned node formula goes deprecated in turn: node@22 did
        (sd:6797). `node` follows the current release and never does."""
        text = (FOLDER / "examples/profiles/common.brew").read_text()
        lines = [ln.split("#")[0].strip() for ln in text.splitlines()]
        nodes = [ln for ln in lines if re.fullmatch(r"node(@[0-9]+)?", ln)]
        self.assertEqual(nodes, ["node"])

    def test_a_versioned_keg_runs_its_own_npm(self):
        (self.profiles / "common.brew").write_text("git\nnode@24\n")
        self.npm.unlink()
        keg = self.prefix / "opt/node@24/bin"
        write_exec(keg / "npm", NPM_SCRIPT)
        write_exec(keg / "node", NODE_STUB)
        (self.profiles / "personal.npm").write_text(f"{ABSENT}\n")

        out = self.tooling("--apply")

        self.assertEqual(self.installs(), [f"install -g {ABSENT}"], out)

    def test_npm_runs_on_the_node_of_its_own_keg(self):
        """A keg-only node@N is off PATH: `env node` must not find fnm's."""
        wrong = self.stubs.parent / "wrong-node.log"
        write_exec(self.stubs / "node", f'#!/bin/sh\necho "$*" >> "{wrong}"\nexit 1\n')
        (self.profiles / "personal.npm").write_text(f"{PRESENT}\n{ABSENT}\n")

        out = self.tooling("--apply")

        self.assertFalse(wrong.exists(), out)
        self.assertIn(f"  ok      npm {PRESENT}", out)
        self.assertEqual(self.installs(), [f"install -g {ABSENT}"], out)

    def test_a_present_package_counts_by_its_path_not_the_exit_code(self):
        """npm 11 exits 1 for a present package when the tree has problems."""
        (self.profiles / "personal.npm").write_text(f"{PRESENT}\n")
        self.ls_rc = 1

        out = self.tooling("--apply")

        self.assertIn(f"  ok      npm {PRESENT}", out)
        self.assertEqual(self.installs(), [], out)

    def test_a_miss_with_exit_0_is_still_missing(self):
        """npm 10 exits 0 and prints nothing for a package it does not have."""
        (self.profiles / "personal.npm").write_text(f"{ABSENT}\n")

        out = self.tooling()

        self.assertIn(f"  MISSING npm {ABSENT}", out)

    def test_a_failed_probe_is_not_a_miss(self):
        (self.profiles / "personal.npm").write_text(f"{ABSENT}\n")
        self.ls_rc = 2

        out = self.tooling("--apply")

        self.assertIn(f"FAILED  npm ls -g {ABSENT}", out)
        self.assertNotIn(f"MISSING npm {ABSENT}", out)
        self.assertEqual(self.installs(), [], out)

    def test_no_homebrew_node_npm_is_missing(self):
        (self.profiles / "personal.npm").write_text(f"{ABSENT}\n")
        self.npm.unlink()

        out = self.tooling("--apply")

        self.assertIn("  MISSING npm (", out)
        self.assertEqual(self.installs(), [], out)

    def test_no_npm_file_runs_no_npm(self):
        out = self.tooling("--apply")

        self.assertNotIn("npm", out)
        self.assertFalse(self.npm_log.exists(), out)

    def test_report_lists_the_npm_kind(self):
        (self.profiles / "personal.npm").write_text(f"{ABSENT}\n")

        result = self.run_verb("report", "personal")

        self.assertIn("    npm (1)", result.stdout, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
