"""brew-doctor.sh run: each brew call is bounded, and a call that did not
answer is not read as an answer (sd:3536).

The script runs from a copy beside a copy of lib/; `brew` is a stub on PATH
that answers from BREW_* variables and counts its `doctor` runs.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from datetime import date

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"

# The first doctor run warns of an unlinked keg and exits BREW_FIRST_RC; the
# second prints BREW_RECHECK and exits BREW_RECHECK_RC. `info` exits BREW_INFO_RC, or ends
# itself with a TERM when that is "term"; `info --json=v2` prints BREW_INFO_JSON.
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
  info) if [ "$2" = --json=v2 ]; then printf '%s' "$BREW_INFO_JSON"; exit 0; fi
        [ "$BREW_INFO_RC" != term ] || kill -TERM $$
        exit "${BREW_INFO_RC:-0}" ;;
  link) echo "link $2" >> "$LINKS" ;;
esac
exit 0
"""

# Homebrew's text on an Intel Mac since September 2026, shortened.
INTEL_WARNING = ("Warning: You are using macOS on Intel x86_64.\n"
                 "We do not provide support for this platform.\n\n"
                 "This is a Tier 3 configuration:\n  https://docs.brew.sh/Support-Tiers#tier-3\n\n")

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

    def test_the_intel_platform_warning_alone_is_clean(self):
        """Homebrew warns every night on an Intel Mac; no fix clears it (sd:4785)."""
        run = self.run_doctor(BREW_RECHECK=INTEL_WARNING, BREW_RECHECK_RC="1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("brew doctor: clean after auto-fixes", run.stdout)
        self.assertNotIn("SD_REPORT_ATTENTION", run.stdout)

    def test_the_intel_platform_warning_leaves_other_warnings_reported(self):
        run = self.run_doctor(BREW_RECHECK=INTEL_WARNING + "Warning: Some taps are not trusted.\n  example/tap\n",
                              BREW_RECHECK_RC="1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("SD_REPORT_ATTENTION: brew doctor on", run.stdout)
        self.assertIn("Some taps are not trusted.", run.stdout)
        self.assertNotIn("Intel", run.stdout)

    def test_a_keg_whose_info_did_not_answer_is_not_called_an_orphan(self):
        run = self.run_doctor(BREW_INFO_RC="term")
        self.assertIn("skip: brew info example-keg did not answer (exit 143)", run.stdout)
        self.assertNotIn("orphan", run.stdout)
        self.assertFalse(self.links.exists())

    def test_a_deprecation_disabled_months_away_is_not_reported(self):
        """icu4c@78 and node@22 alerted nightly a year before their disable date (sd:6795)."""
        soon = date.today().isoformat()
        info = {"formulae": [{"name": "far-formula", "disabled": False, "disable_date": "2099-01-01"},
                             {"name": "near-formula", "disabled": False, "disable_date": soon},
                             {"name": "gone-formula", "disabled": True, "disable_date": "2099-01-01"}],
                "casks": [{"token": "far-cask", "disabled": False, "disable_date": "2099-01-01"}]}
        recheck = ("Warning: Some installed formulae are deprecated or disabled.\n"
                   "You should find replacements for the following formulae:\n"
                   "  far-formula\n  near-formula\n  gone-formula\n\n"
                   "Warning: Some installed casks are deprecated or disabled.\n"
                   "You should find replacements for the following casks:\n  far-cask\n\n")
        run = self.run_doctor(BREW_RECHECK=recheck, BREW_RECHECK_RC="1", BREW_INFO_JSON=json.dumps(info))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("formulae are deprecated or disabled.: near-formula", run.stdout)
        self.assertIn("formulae are deprecated or disabled.: gone-formula", run.stdout)
        self.assertNotIn("far-formula", run.stdout)
        self.assertNotIn("casks are deprecated", run.stdout)

    def test_only_distant_deprecations_are_clean(self):
        info = {"formulae": [{"name": "far-formula", "disabled": False, "disable_date": "2099-01-01"}], "casks": []}
        recheck = ("Warning: Some installed formulae are deprecated or disabled.\n"
                   "You should find replacements for the following formulae:\n  far-formula\n")
        run = self.run_doctor(BREW_RECHECK=recheck, BREW_RECHECK_RC="1", BREW_INFO_JSON=json.dumps(info))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("brew doctor: clean after auto-fixes", run.stdout)
        self.assertNotIn("SD_REPORT_ATTENTION", run.stdout)

    def test_a_deprecation_whose_info_did_not_answer_stays_reported(self):
        recheck = ("Warning: Some installed formulae are deprecated or disabled.\n"
                   "You should find replacements for the following formulae:\n  far-formula\n")
        run = self.run_doctor(BREW_RECHECK=recheck, BREW_RECHECK_RC="1", BREW_INFO_JSON="not json")
        self.assertIn("formulae are deprecated or disabled.: far-formula", run.stdout)


if __name__ == "__main__":
    unittest.main()
