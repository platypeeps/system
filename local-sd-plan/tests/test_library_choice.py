"""Which `sd_db` `nightly` imports: the checkout's or the installed copy (sd:1812).

`nightly` put this repository's `local-sd-db` on `PYTHONPATH`, so a primary
checkout one migration behind the live database refused the night, while the
interpreter it ran held a copy that could open it. `sd-db.sh library` makes
the choice its own database verbs make (sd:1765), and `nightly` follows it.

The installed copy is a stand-in in a real virtualenv, since `sd-db.sh` probes
it with `-I`. Its package prints a marker when `sd_plan.py` imports it, which
is how a case tells which library answered.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sd_db.schema import SCHEMA_VERSION

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "sd-plan.sh"
MARKER = "the installed stand-in answered"

# Quiet under the probe, which imports `sd_db.schema` and so this file too.
STAND_IN_INIT = f"""\
import sys
if sys.argv[0].endswith("sd_plan.py"):
    print({MARKER!r}, *sys.argv[1:])
    raise SystemExit(0)
"""


def make_venv(root: Path, schema: int) -> Path:
    """A virtualenv whose site-packages holds a stand-in built for `schema`."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)],
                   check=True, capture_output=True)
    python = root / "bin/python"
    purelib = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True).stdout.strip()
    for name, text in {"sd_db/__init__.py": STAND_IN_INIT,
                       "sd_db/schema.py": f"SCHEMA_VERSION = {schema}\n"}.items():
        path = Path(purelib) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return python


class NightlyFollowsTheLibraryChoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.venvs = tempfile.TemporaryDirectory()
        cls.newer = make_venv(Path(cls.venvs.name) / "newer", SCHEMA_VERSION + 1)

    @classmethod
    def tearDownClass(cls):
        cls.venvs.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def nightly(self, **extra):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("PYTHONPATH", "SD_DB_LIBRARY", "SD_PLAN_PROFILE")}
        # No profile: no participation list, so no runner probe, and the
        # night goes straight to `sd_plan.py`, which imports `sd_db` first.
        environment.update(HOME=str(self.home), PYTHON=str(self.newer),
                           MACHINE_SETUP_STATE=str(self.home / "absent"), **extra)
        return subprocess.run(["/bin/sh", str(ENTRYPOINT), "nightly", "--dry-run"],
                              capture_output=True, text=True, input="", env=environment,
                              check=False, timeout=60)

    def test_a_newer_installed_copy_answers_and_says_so(self):
        done = self.nightly()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"{MARKER} nightly --dry-run", done.stdout)
        self.assertIn(f"one built for {SCHEMA_VERSION + 1}", done.stderr)

    def test_the_checkout_can_still_be_forced(self):
        done = self.nightly(SD_DB_LIBRARY="checkout")
        self.assertNotIn(MARKER, done.stdout)


if __name__ == "__main__":
    unittest.main()
