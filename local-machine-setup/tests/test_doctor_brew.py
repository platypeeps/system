"""doctor's brew doctor line ignores Homebrew's Intel platform warning (sd:4943).

Homebrew warns on every Intel Mac since September 2026, and no fix clears
it; local-brew-doctor drops it already (sd:4785), through the same filter in
lib/brew_unlinked.sh. `brew` is a stub on PATH that prints $BREW_DOCTOR for
`brew doctor` and exits $BREW_DOCTOR_RC.
"""

import unittest

from tests.test_profile_prefs import Case, write_exec

BREW_STUB = r"""#!/bin/sh
echo "brew $*" >> "$CALL_LOG"
[ "$1" = doctor ] || exit 1
printf '%s' "$BREW_DOCTOR"
exit "$BREW_DOCTOR_RC"
"""

# Homebrew's text on an Intel Mac since September 2026, shortened.
INTEL_WARNING = ("Warning: You are using macOS on Intel x86_64.\n"
                 "We do not provide support for this platform.\n\n"
                 "This is a Tier 3 configuration:\n  https://docs.brew.sh/Support-Tiers#tier-3\n\n")


class BrewDoctorLine(Case):
    def doctor(self, output, rc):
        write_exec(self.stubs / "brew", BREW_STUB)
        return self.run_script("doctor", fnm_have="18 20 24", BREW_DOCTOR=output, BREW_DOCTOR_RC=str(rc)).stdout

    def test_the_intel_platform_warning_alone_is_clean(self):
        out = self.doctor(INTEL_WARNING, 1)
        self.assertIn("  ok      brew doctor clean", out)
        self.assertNotIn("brew doctor has warnings", out)

    def test_another_warning_beside_it_still_warns(self):
        out = self.doctor(INTEL_WARNING + "Warning: Some taps are not trusted.\n  example/tap\n", 1)
        self.assertIn("  WARN    brew doctor has warnings", out)

    def test_a_doctor_that_did_not_answer_still_warns(self):
        """A timeout is no answer, even after the platform warning."""
        out = self.doctor(INTEL_WARNING, 124)
        self.assertIn("  WARN    brew doctor has warnings", out)


if __name__ == "__main__":
    unittest.main()
