"""Entrypoints answer `help`, `-h` and `--help` with usage text and exit 0 (sd:3554).

Convention 1 asks it of every script. Two scripts ignored it: a bridge name
lookup took `help` for a name, and the roster capture ran on any argument. The
scripts run with HOME and the config folder in a scratch directory, so the
failing form of either one touches nothing real. Stdlib only, so it runs in
the `tests/ci-native.sh` preflight.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPTS = (
    "network-testing/thunderbolt-bridge.sh",
    "local-machine-setup/profile-autocapture.sh",
    "local-machine-setup/sudo-askpass.sh",
    "local-machine-setup/examples/dotfiles/work/.local/bin/sdm-login",
)


class HelpForms(unittest.TestCase):
    def test_each_form_prints_usage_and_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ["PATH"], "HOME": tmp,
                   "SYSTEM_TOOLS_CONFIG": str(Path(tmp) / "config")}
            for script in SCRIPTS:
                for form in ("help", "-h", "--help"):
                    with self.subTest(script=script, form=form):
                        done = subprocess.run(
                            ["sh", str(REPO_ROOT / script), form], env=env, cwd=tmp,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
                        )
                        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                        self.assertIn("usage", done.stdout.lower())
                        self.assertEqual(done.stderr, "")


if __name__ == "__main__":
    unittest.main()
