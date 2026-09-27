"""Native retention: `storage.retain` freezes a clone with the user immutable flag.

`chflags uchg` is macOS-only, so this cannot run on the Linux CI runner.
tests/run-macos-only.sh runs it on a Mac; the storage policy tests that mock
the platform stay in tests/test_runtime.py.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sd_runner import storage


class NativeRetention(unittest.TestCase):
    def test_native_immutable_retention_and_thawed_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            clone = root / "active"
            clone.mkdir()
            (clone / "precious").write_text("immutable original")
            retained = root / "retention/1/1/clone"
            try:
                storage.retain(clone, retained)
                with self.assertRaises(PermissionError):
                    (retained / "precious").write_text("must be refused")
                restored = storage.restore({"id": "1" * 32, "retained_path": str(retained)}, root / "restored")
                (restored / "precious").write_text("operator can edit restored copy")
                self.assertEqual((retained / "precious").read_text(), "immutable original")
            finally:
                if retained.exists():
                    subprocess.run(["chflags", "-R", "nouchg", str(retained)], check=True)


if __name__ == "__main__":
    unittest.main()
