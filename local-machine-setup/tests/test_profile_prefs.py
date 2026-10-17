"""The node versions and the mailto handler come from the profile (sd:3966).

`<profile>.node` lists the node majors fnm keeps, one per line, and
`<profile>.mailto` names the app that answers mailto: links by bundle id. A
profile with neither file keeps the values every machine had before: node 18,
20 and 24, and Chrome.

Each case runs machine-setup.sh from a copy of this folder. `fnm`, `duti` and
`defaults` are stubs on PATH that log their calls; fixture_config.seal fails a
case that reaches a real `sudo` or `launchctl`.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# `fnm ls` lists the majors in $FNM_HAVE; every other call is logged.
FNM_STUB = r"""#!/bin/sh
echo "fnm $*" >> "$CALL_LOG"
if [ "$1" = ls ]; then
  for v in $FNM_HAVE; do echo "* v$v.0.0"; done
fi
exit 0
"""

DUTI_STUB = r"""#!/bin/sh
echo "duti $*" >> "$CALL_LOG"
exit 0
"""

# The LaunchServices read answers with $LS_MAILTO as the mailto handler.
DEFAULTS_STUB = r"""#!/bin/sh
echo "defaults $*" >> "$CALL_LOG"
case "$*" in
  "read com.apple.LaunchServices/com.apple.launchservices.secure LSHandlers")
    printf '    {\n        LSHandlerRoleAll = "%s";\n        LSHandlerURLScheme = mailto;\n    },\n' "$LS_MAILTO"
    exit 0 ;;
esac
exit 1
"""

# Logs every call; lists nothing.
LOGGING_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALL_LOG"
exit 1
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        repo = self.base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config = fixture_config.copy_config(self.base)
        self.profiles = self.config / "machine-setup/profiles"
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.call_log = self.base / "calls.log"
        self.call_log.touch()
        self.stubs = self.base / "stubs"
        write_exec(self.stubs / "fnm", FNM_STUB)
        write_exec(self.stubs / "duti", DUTI_STUB)
        write_exec(self.stubs / "defaults", DEFAULTS_STUB)
        for name in ("brew", "sudo", "launchctl", "git", "docker", "tailscale", "mas"):
            write_exec(self.stubs / name, LOGGING_STUB)
        fixture_config.seal(self, self.stubs)

    def run_script(self, *args, fnm_have="", ls_mailto="com.google.chrome", **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "CALL_LOG": str(self.call_log),
            "FNM_HAVE": fnm_have,
            "LS_MAILTO": ls_mailto,
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def update_tooling(self, **kw):
        result = self.run_script("update", "tooling", "--apply", **kw)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def calls(self, tool):
        return [c for c in self.call_log.read_text().splitlines() if c.startswith(tool + " ")]


class NodeVersions(Case):
    def test_a_profile_naming_node_22_installs_22_alone(self):
        (self.profiles / "personal.node").write_text("# the one node this machine needs\n22\n")
        out = self.update_tooling()
        self.assertEqual(self.calls("fnm install"), ["fnm install 22"])
        self.assertIn("  MISSING node 22 (fnm)", out)
        self.assertNotIn("node 18", out)

    def test_common_and_profile_node_files_add_up(self):
        (self.profiles / "common.node").write_text("20\n")
        (self.profiles / "personal.node").write_text("22\n")
        self.update_tooling(fnm_have="20")
        self.assertEqual(self.calls("fnm install"), ["fnm install 22"])

    def test_no_node_file_keeps_18_20_and_24(self):
        out = self.update_tooling(fnm_have="20")
        self.assertEqual(self.calls("fnm install"), ["fnm install 18", "fnm install 24"])
        self.assertIn("  ok      node 20 (fnm)", out)

    def test_doctor_checks_the_profiles_node_versions(self):
        (self.profiles / "personal.node").write_text("22\n")
        out = self.run_script("doctor", fnm_have="22").stdout
        self.assertIn("  ok      fnm node 22 installed", out)
        self.assertNotIn("fnm node 18", out)

    def test_doctor_names_the_cask_when_claude_is_missing(self):
        """sd:4738: the stubs folder has no claude, so doctor names the cask."""
        out = self.run_script("doctor", fnm_have="20").stdout
        self.assertIn("  WARN    claude missing — run: brew install --cask claude-code@latest", out)

    def test_doctor_without_a_node_file_checks_18_20_and_24(self):
        out = self.run_script("doctor", fnm_have="20").stdout
        self.assertIn("  WARN    fnm node 18 missing — run: fnm install 18", out)
        self.assertIn("  ok      fnm node 20 installed", out)
        self.assertIn("  WARN    fnm node 24 missing — run: fnm install 24", out)


class MailtoHandler(Case):
    def test_a_profile_handler_is_set_when_another_app_holds_mailto(self):
        (self.profiles / "personal.mailto").write_text("com.example.mail\n")
        out = self.update_tooling(fnm_have="18 20 24")
        self.assertIn("  DIFFERS mailto handler is not com.example.mail", out)
        self.assertEqual(self.calls("duti"), ["duti -s com.example.mail mailto:"])

    def test_a_profile_handler_already_set_is_ok(self):
        (self.profiles / "personal.mailto").write_text("com.example.mail\n")
        out = self.update_tooling(fnm_have="18 20 24", ls_mailto="com.example.mail")
        self.assertIn("  ok      mailto handler is com.example.mail", out)
        self.assertEqual(self.calls("duti"), [])

    def test_the_profile_handler_wins_over_common(self):
        (self.profiles / "common.mailto").write_text("com.example.common\n")
        (self.profiles / "personal.mailto").write_text("com.example.mail\n")
        self.update_tooling(fnm_have="18 20 24", ls_mailto="com.example.common")
        self.assertEqual(self.calls("duti"), ["duti -s com.example.mail mailto:"])

    def test_a_bang_line_alone_drops_common_s_handler_for_chrome(self):
        """sd:4942: `!id` drops common's id, as it drops any other kind's
        entry; skipping it alone let common's handler win."""
        (self.profiles / "common.mailto").write_text("com.example.common\n")
        (self.profiles / "personal.mailto").write_text("!com.example.common\n")
        self.update_tooling(fnm_have="18 20 24", ls_mailto="com.example.common")
        self.assertEqual(self.calls("duti"), ["duti -s com.google.Chrome mailto:"])

    def test_no_mailto_file_keeps_chrome(self):
        out = self.update_tooling(fnm_have="18 20 24")
        self.assertIn("  ok      mailto handler is com.google.Chrome", out)
        self.assertEqual(self.calls("duti"), [])

    def test_no_mailto_file_sets_chrome_when_another_app_holds_mailto(self):
        out = self.update_tooling(fnm_have="18 20 24", ls_mailto="com.apple.mail")
        self.assertIn("  DIFFERS mailto handler is not com.google.Chrome", out)
        self.assertEqual(self.calls("duti"), ["duti -s com.google.Chrome mailto:"])


class Examples(unittest.TestCase):
    def test_the_examples_carry_both_kinds(self):
        examples = FOLDER / "examples/profiles"
        def entries(name):
            lines = (examples / name).read_text().splitlines()
            return [l for l in lines if l and not l.startswith("#")]
        self.assertEqual(entries("laptop.node"), ["18", "20", "24"])
        self.assertEqual(entries("laptop.mailto"), ["com.google.Chrome"])


if __name__ == "__main__":
    unittest.main()
