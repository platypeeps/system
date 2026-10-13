"""brew-doctor.sh run: each brew call is bounded, and a call that did not
answer is not read as an answer (sd:3536).

The script runs from a copy beside a copy of lib/; `brew` is a stub on PATH
that answers from BREW_* variables and counts its `doctor` runs.
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"

# The first doctor run warns of an unlinked keg and exits BREW_FIRST_RC; the
# second prints BREW_RECHECK and exits BREW_RECHECK_RC. `info` exits BREW_INFO_RC, or ends
# itself with a TERM when that is "term".
BREW_STUB = r"""#!/bin/sh
case "$1" in
  doctor)
    n=$(cat "$COUNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$COUNT"
    if [ "$n" -eq 1 ]; then
      printf 'Warning: You have unlinked kegs in your Cellar.\n  example-keg\n\n' >&2
      exit "${BREW_FIRST_RC:-1}"
    fi
    printf '%b' "$BREW_RECHECK" >&2
    exit "${BREW_RECHECK_RC:-0}" ;;
  info) [ "$BREW_INFO_RC" != term ] || kill -TERM $$
        exit "${BREW_INFO_RC:-0}" ;;
  link) echo "link $2" >> "$LINKS" ;;
esac
exit 0
"""


class BrewDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="brew-doctor."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "local-brew-doctor").mkdir()
        shutil.copy(FOLDER / "brew-doctor.sh", self.tmp / "local-brew-doctor")
        shutil.copytree(LIB, self.tmp / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        brew = self.bin / "brew"
        brew.write_text(BREW_STUB)
        brew.chmod(0o755)
        self.links = self.tmp / "links"
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp),
                    "COUNT": str(self.tmp / "count"), "LINKS": str(self.links)}

    def run_doctor(self, **extra):
        return subprocess.run(["sh", str(self.tmp / "local-brew-doctor" / "brew-doctor.sh"), "run"],
                              capture_output=True, text=True, env={**self.env, **extra},
                              stdin=subprocess.DEVNULL, timeout=60)

    def test_each_brew_call_names_itself_in_the_log_not_the_report(self):
        run = self.run_doctor()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("autofix: brew link example-keg", run.stdout)
        self.assertIn("brew doctor: clean after auto-fixes", run.stdout)
        self.assertRegex(run.stderr, r"\[step [\d:]+\] brew doctor \(bound 600s\)")
        self.assertRegex(run.stderr, r"\[step [\d:]+\] brew link example-keg \(bound 300s\)")
        self.assertNotIn("[step ", run.stdout)

    def test_a_recheck_that_fails_without_a_warning_is_not_clean(self):
        """A timed-out re-check printed 'clean after auto-fixes'."""
        run = self.run_doctor(BREW_RECHECK="Error: cannot read the Cellar\n", BREW_RECHECK_RC="124")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertNotIn("clean", run.stdout)
        self.assertIn("brew doctor re-check FAILED (exit 124)", run.stderr)

    def test_a_doctor_run_cut_off_after_a_warning_fixes_nothing(self):
        """A partial warning before a timeout is no answer to act on."""
        run = self.run_doctor(BREW_FIRST_RC="124")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("brew doctor FAILED (exit 124)", run.stderr)
        self.assertFalse(self.links.exists())
        pathlib.Path(self.env["COUNT"]).unlink()
        recheck = self.run_doctor(BREW_RECHECK="Warning: partial\n", BREW_RECHECK_RC="124")
        self.assertEqual(recheck.returncode, 1, recheck.stdout + recheck.stderr)
        self.assertIn("brew doctor re-check FAILED (exit 124)", recheck.stderr)

    def test_a_keg_whose_info_did_not_answer_is_not_called_an_orphan(self):
        run = self.run_doctor(BREW_INFO_RC="term")
        self.assertIn("skip: brew info example-keg did not answer (exit 143)", run.stdout)
        self.assertNotIn("orphan", run.stdout)
        self.assertFalse(self.links.exists())


if __name__ == "__main__":
    unittest.main()
