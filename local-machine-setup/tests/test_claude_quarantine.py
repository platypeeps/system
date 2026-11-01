"""The cask's claude keeps no quarantine that Gatekeeper would ask about (sd:4787).

A new claude-code@latest cask version carries com.apple.quarantine. Its first
run under launchd, with the screen locked, waited on a Gatekeeper prompt no one
could answer, and every agent job behind it hit its limit. The operator's
ruling: after each install or upgrade, check the signature, the notarization
and the team; clear the flag only when all three pass, else keep it and say so.

The codex cask's bin/ files carry the same flag after each upgrade, and
Gatekeeper blocked codex-code-mode-host (sd:7095): the same rule clears each
of them, with OpenAI's team.

The script runs from a copy of this folder under a temporary root. `brew`,
`mas`, `codesign`, `spctl` and `xattr` are stubs on PATH; `claude` and
`codex` on PATH link into a temporary Caskroom, as the casks link them.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config
from tests.fixture_config import STALL, STALL_BOUND

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"
TEAM = "Q6L2SF6YDW"
CODEX_TEAM = "2DC432GLL2"

BREW_STUB = r"""#!/bin/sh
echo "brew $*" >> "$CALLS"
[ "$*" != "outdated --cask --greedy --quiet" ] || echo claude-code@latest
exit 0
"""

# A file's quarantine is a file beside it, `<path>.quarantine`.
# XATTR_FAIL fails the read of the names.
XATTR_STUB = r"""#!/bin/sh
echo "xattr $*" >> "$CALLS"
case $1 in
  -d) [ "$2" = com.apple.quarantine ] && rm "$3.quarantine" ;;
  -*) exit 2 ;;
  *) [ -z "$XATTR_FAIL" ] || exit 1
     [ ! -e "$1.quarantine" ] || echo com.apple.quarantine ;;
esac
"""

# Each check passes only for a path in GOOD, a colon-separated list, so a
# check of another binary is no evidence for this one. BAD_SIGNATURE fails
# the verify, CODESIGN_TEAM (claude) and CODEX_SIGN_TEAM (a codex cask file)
# name another team, NOT_NOTARIZED makes Gatekeeper call it unnotarized, and
# SPCTL_HANG stalls (fixture_config.STALL).
CODESIGN_STUB = r"""#!/bin/sh
echo "codesign $*" >> "$CALLS"
for last; do :; done
case ":$GOOD:" in *":$last:"*) ;; *) exit 1 ;; esac
case $last in
  */Caskroom/codex/*) team=${CODEX_SIGN_TEAM:-2DC432GLL2} ;;
  *) team=${CODESIGN_TEAM:-Q6L2SF6YDW} ;;
esac
case $1 in
  --verify) [ -z "$BAD_SIGNATURE" ] ;;
  -dv) echo "Identifier=example.test.binary" >&2
       echo "TeamIdentifier=$team" >&2 ;;
  *) exit 2 ;;
esac
"""

SPCTL_STUB = "#!/bin/sh\n" + STALL + r"""echo "spctl $*" >> "$CALLS"
[ -z "$SPCTL_HANG" ] || stall
for last; do :; done
case ":$GOOD:" in *":$last:"*) good=1 ;; *) good= ;; esac
if [ -n "$good" ] && [ -z "$NOT_NOTARIZED" ]; then
  printf '%s: accepted\nsource=Notarized Developer ID\n' "$last" >&2
else
  printf '%s: rejected\nsource=Unnotarized Developer ID\n' "$last" >&2
  exit 3
fi
"""

EXIT_1_STUB = "#!/bin/sh\nexit 1\n"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class QuarantineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.calls = base / "calls.log"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "mas", "#!/bin/sh\nexit 0\n")
        write_exec(self.stubs / "xattr", XATTR_STUB)
        write_exec(self.stubs / "codesign", CODESIGN_STUB)
        write_exec(self.stubs / "spctl", SPCTL_STUB)
        for name in ("defaults", "sudo", "launchctl"):
            write_exec(self.stubs / name, EXIT_1_STUB)
        fixture_config.seal(self, self.stubs)
        caskroom = base / "Caskroom/claude-code@latest"
        self.old = caskroom / "2.1.300/claude"
        self.claude = caskroom / "2.1.301/claude"
        for binary in (self.old, self.claude):
            write_exec(binary, "#!/bin/sh\nexit 0\n")
        (self.stubs / "claude").symlink_to(self.claude)
        self.quarantine = pathlib.Path(f"{self.claude}.quarantine")
        self.quarantine.touch()

    def run_verb(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "CALLS": str(self.calls),
            "GOOD": str(self.claude),
            "ST_BOUNDED_GRACE": "1",
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=60)

    def calls_of(self, tool):
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        return [line for line in lines if line.split(" ", 1)[0] == tool]


class ClaudeQuarantineTest(QuarantineCase):
    def assert_cleared(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"claude quarantined; signed, notarized, team {TEAM}: clearing", result.stdout)
        self.assertFalse(self.quarantine.exists(), result.stdout)
        self.assertEqual(self.calls_of("xattr")[-1], f"xattr -d com.apple.quarantine {self.claude}")

    def assert_kept(self, result, why):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"DIFFERS claude quarantine kept on {self.claude}: {why}", result.stdout)
        self.assertTrue(self.quarantine.exists(), result.stdout)
        self.assertNotIn(f"xattr -d com.apple.quarantine {self.claude}", self.calls_of("xattr"))

    # --- every check passes: the flag goes

    def test_the_weekly_upgrade_clears_a_verified_quarantine(self):
        result = self.run_verb("upgrade", "--apply")

        self.assert_cleared(result)
        calls = self.calls.read_text().splitlines()
        # After the cask upgrade, not before it: the new version is the one to check.
        self.assertLess(calls.index("brew upgrade --cask --greedy claude-code@latest"),
                        calls.index(f"xattr -d com.apple.quarantine {self.claude}"))

    def test_the_brew_stage_clears_a_verified_quarantine(self):
        """The nightly `update --apply` runs the brew stage: a cask installed
        or upgraded by hand, or before this change, is cleared there."""
        result = self.run_verb("update", "brew", "--apply")

        self.assert_cleared(result)
        self.assertIn(f"spctl --assess --type open --context context:primary-signature -v {self.claude}",
                      self.calls_of("spctl"))

    def test_the_checks_are_bounded(self):
        result = self.run_verb("update", "brew", "--apply")

        for command in ("codesign --verify --strict", "spctl --assess", "codesign -dv"):
            self.assertRegex(result.stderr, rf"\[step [\d:]+\] {command} .*\(bound 120s\)")

    def test_a_dry_run_checks_and_clears_nothing(self):
        result = self.run_verb("update", "brew")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"[dry-run] xattr -d com.apple.quarantine {self.claude}", result.stdout)
        self.assertTrue(self.quarantine.exists())

    # --- any check fails: the flag stays, and the run says so

    def test_a_bad_signature_keeps_the_quarantine(self):
        result = self.run_verb("upgrade", "--apply", BAD_SIGNATURE="1")

        self.assert_kept(result, "the signature does not verify")
        self.assertIn("failed steps: claude quarantine", result.stdout)

    def test_an_unnotarized_binary_keeps_the_quarantine(self):
        result = self.run_verb("update", "brew", "--apply", NOT_NOTARIZED="1")

        self.assert_kept(result, "Gatekeeper did not say it is notarized")
        self.assertIn("FAILED  brew stage: claude keeps com.apple.quarantine", result.stdout)

    def test_another_team_keeps_the_quarantine(self):
        result = self.run_verb("update", "brew", "--apply", CODESIGN_TEAM="EXAMPLE123")

        self.assert_kept(result, f"its team is not {TEAM}")

    def test_checks_that_pass_for_another_version_keep_the_quarantine(self):
        """Evidence: every check must be of the binary whose flag goes."""
        result = self.run_verb("upgrade", "--apply", GOOD=str(self.old))

        self.assert_kept(result, "the signature does not verify")

    def test_a_check_that_does_not_answer_keeps_the_quarantine(self):
        result = self.run_verb("update", "brew", "--apply", SPCTL_HANG="1",
                               MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND))

        self.assert_kept(result, "Gatekeeper did not say it is notarized")
        self.assertRegex(result.stderr, r"\[step [\d:]+\] spctl .* exited 124")

    def test_an_unreadable_flag_is_unknown_not_clear(self):
        result = self.run_verb("update", "brew", "--apply", XATTR_FAIL="1")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"DIFFERS claude quarantine unknown: xattr could not read {self.claude}", result.stdout)
        self.assertEqual(self.calls_of("codesign"), [])
        self.assertTrue(self.quarantine.exists())

    # --- nothing to clear

    def test_no_quarantine_runs_no_check(self):
        self.quarantine.unlink()
        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok      claude not quarantined", result.stdout)
        self.assertEqual(self.calls_of("codesign") + self.calls_of("spctl"), [])
        self.assertEqual(self.calls_of("xattr"), [f"xattr {self.claude}"])

    def test_a_claude_outside_the_cask_is_left_alone(self):
        (self.stubs / "claude").unlink()
        write_exec(self.stubs / "claude", "#!/bin/sh\nexit 0\n")
        native = self.run_verb("update", "brew", "--apply")
        (self.stubs / "claude").unlink()
        none = self.run_verb("upgrade", "--apply")

        for result in (native, none):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("claude", result.stdout.replace(str(self.folder), ""))
        self.assertEqual(self.calls_of("xattr") + self.calls_of("codesign") + self.calls_of("spctl"), [])


class CodexQuarantineTest(QuarantineCase):
    """The codex cask: every file in the bin/ of the version `codex` on PATH
    links to, each checked against OpenAI's team (sd:7095)."""

    def setUp(self):
        super().setUp()
        # Only codex is quarantined here; ClaudeQuarantineTest covers claude.
        self.quarantine.unlink()
        caskroom = pathlib.Path(self.tmp.name) / "Caskroom/codex"
        self.old_codex = caskroom / "0.164.0/bin/codex"
        bin_dir = caskroom / "0.165.0/bin"
        self.codex = [bin_dir / name for name in ("codex", "codex-code-mode-host", "codex-exec-server")]
        for binary in (self.old_codex, *self.codex):
            write_exec(binary, "#!/bin/sh\nexit 0\n")
            pathlib.Path(f"{binary}.quarantine").touch()
        (self.stubs / "codex").symlink_to(self.codex[0])

    def run_verb(self, *args, **extra):
        good = ":".join(str(path) for path in (self.claude, self.old_codex, *self.codex))
        return super().run_verb(*args, **{"GOOD": good, **extra})

    def flagged(self, path):
        return pathlib.Path(f"{path}.quarantine").exists()

    def assert_codex_cleared(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in self.codex:
            self.assertIn(f"{path.name} quarantined; signed, notarized, team {CODEX_TEAM}: clearing",
                          result.stdout)
            self.assertFalse(self.flagged(path), result.stdout)
            self.assertIn(f"xattr -d com.apple.quarantine {path}", self.calls_of("xattr"))
            self.assertIn(f"codesign --verify --strict {path}", self.calls_of("codesign"))
        # Another version's files are not the ones `codex` runs.
        self.assertTrue(self.flagged(self.old_codex))
        self.assertNotIn(str(self.old_codex), self.calls.read_text())

    def test_the_weekly_upgrade_clears_every_codex_bin_file(self):
        result = self.run_verb("upgrade", "--apply")

        self.assert_codex_cleared(result)
        calls = self.calls.read_text().splitlines()
        self.assertLess(calls.index("brew upgrade --cask --greedy claude-code@latest"),
                        calls.index(f"xattr -d com.apple.quarantine {self.codex[0]}"))

    def test_the_brew_stage_clears_every_codex_bin_file(self):
        result = self.run_verb("update", "brew", "--apply")

        self.assert_codex_cleared(result)

    def test_another_team_keeps_the_codex_quarantine(self):
        """Anthropic's team is a real team, and not OpenAI's."""
        stage = self.run_verb("update", "brew", "--apply", CODEX_SIGN_TEAM=TEAM)
        upgrade = self.run_verb("upgrade", "--apply", CODEX_SIGN_TEAM=TEAM)

        for result in (stage, upgrade):
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            for path in self.codex:
                self.assertIn(f"DIFFERS {path.name} quarantine kept on {path}: its team is not {CODEX_TEAM}",
                              result.stdout)
                self.assertTrue(self.flagged(path), result.stdout)
        self.assertIn("FAILED  brew stage: codex keeps com.apple.quarantine", stage.stdout)
        self.assertIn("failed steps: codex quarantine", upgrade.stdout)
        self.assertEqual([c for c in self.calls_of("xattr") if c.startswith("xattr -d")], [])

    def test_no_codex_cask_is_silent(self):
        (self.stubs / "codex").unlink()
        none = self.run_verb("upgrade", "--apply")
        write_exec(self.stubs / "codex", "#!/bin/sh\nexit 0\n")
        native = self.run_verb("update", "brew", "--apply")

        for result in (none, native):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("codex", result.stdout.replace(str(self.folder), ""))
        self.assertNotIn("Caskroom/codex", self.calls.read_text())


if __name__ == "__main__":
    unittest.main()
