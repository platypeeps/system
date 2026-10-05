"""A formula named both bare and tap-qualified is in sync, not missing.

`brew list --formula` prints bare names but tap-qualifies a formula whose name
is ambiguous, so `brew leaves` can print `owner/tap/name` for something
`common.brew` already names as `name`. Two places read that:

* `capture` subtracted the common manifest from `brew leaves` without
  normalizing either side, so the tap-qualified spelling never matched
  common's bare one and capture wrote a second entry for one formula.
  It compares bare names now, but writes the spelling brew printed: a bare
  name for a third-party formula would install a core one of the same name.
* `status` strips the tap prefix, but `manifest` deduplicates before that
  strip. Both spellings therefore collapsed to the same bare name twice in
  the wanted set, and `comm` reported the duplicate as a formula that was
  missing while it was installed all along.

The stub `brew` answers as a machine with the tap would: bare in
`list --formula`, tap-qualified in `leaves`.
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

TAP = "example-owner/tap"
FORMULA = "example-formula"
QUALIFIED = f"{TAP}/{FORMULA}"

# Installed, and reported the way brew actually reports it: bare from
# `list --formula`, tap-qualified from `leaves`.
BREW_STUB = f"""#!/bin/sh
case "$*" in
  "list --formula") printf '%s\\n' {FORMULA} ;;
  "list --cask") : ;;
  leaves) printf '%s\\n' {QUALIFIED} ;;
  tap) printf '%s\\n' {TAP} ;;
  --prefix) echo /usr/local ;;
esac
exit 0
"""

EXIT_1_STUB = "#!/bin/sh\nexit 1\n"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TapQualifiedFormula(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder,
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))

        self.config_root = fixture_config.copy_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")

        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        for name in ("defaults", "sudo", "launchctl", "mas"):
            write_exec(self.stubs / name, EXIT_1_STUB)
        fixture_config.seal(self, self.stubs)

    def env(self):
        return {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
        }

    def run_verb(self, *args):
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args],
                              env=self.env(), capture_output=True, text=True,
                              cwd=self.tmp.name, stdin=subprocess.DEVNULL, timeout=120)

    def brew_section(self, out):
        """The lines the brew formulae stage printed, without the heading."""
        lines = out.splitlines()
        start = lines.index("brew formulae")
        rest = lines[start + 1:]
        end = next((i for i, l in enumerate(rest) if not l.startswith("  ")), len(rest))
        return rest[:end]

    def test_both_spellings_of_one_formula_report_in_sync(self):
        (self.profiles / "common.brew").write_text(f"{FORMULA}\n")
        (self.profiles / "personal.brew").write_text(f"{QUALIFIED}\n")

        result = self.run_verb("status")

        section = self.brew_section(result.stdout)
        self.assertEqual(section, ["  ok      in sync"],
                         f"brew section was {section!r}\n{result.stdout}")

    def test_the_bare_spelling_alone_still_reports_in_sync(self):
        """The normalization must not depend on a tap-qualified entry existing."""
        (self.profiles / "common.brew").write_text(f"{FORMULA}\n")
        (self.profiles / "personal.brew").write_text("")

        result = self.run_verb("status")

        self.assertEqual(self.brew_section(result.stdout), ["  ok      in sync"],
                         result.stdout)

    def test_a_formula_the_profile_does_not_name_is_still_extra(self):
        """Deduplicating must not hide a real finding."""
        (self.profiles / "common.brew").write_text("")
        (self.profiles / "personal.brew").write_text("")

        result = self.run_verb("status")

        self.assertIn(f"  extra   : {FORMULA}", result.stdout)

    def test_a_formula_nothing_installed_is_still_missing(self):
        (self.profiles / "common.brew").write_text("absent-formula\n")
        (self.profiles / "personal.brew").write_text("")

        result = self.run_verb("status")

        self.assertIn("  missing : absent-formula", result.stdout)


    def profile_brew(self):
        """The written brew manifest. Not the exit code: the fixture's agent
        roster holds its own manifest, which exits non-zero by design."""
        path = self.profiles / "personal.brew"
        return [l for l in path.read_text().splitlines()
                if l.strip() and not l.startswith("#")]

    def test_capture_does_not_repeat_a_formula_common_names_bare(self):
        (self.profiles / "common.brew").write_text(f"{FORMULA}\n")
        (self.profiles / "personal.brew").write_text("")

        result = self.run_verb("capture", "--apply", "--additive")

        self.assertEqual(self.profile_brew(), [], result.stdout)

    def test_capture_keeps_the_tap_spelling_of_a_formula_common_lacks(self):
        """A bare name could resolve to a core formula of the same name."""
        (self.profiles / "common.brew").write_text("")
        (self.profiles / "personal.brew").write_text("")

        result = self.run_verb("capture", "--apply", "--additive")

        self.assertEqual(self.profile_brew(), [QUALIFIED], result.stdout)


if __name__ == "__main__":
    unittest.main()
