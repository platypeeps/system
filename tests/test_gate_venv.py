"""`tests/gate-venv.sh`: the gate's venv, built once per key (sd:9926).

tests/check.sh names the venv by the pack's lib/ tree and the python; the
preflight and each macOS-only suite call gate-venv.sh, which reuses a venv
that exists and otherwise builds one beside it and renames it in whole.
A fake python3 records each build, so this runs in a second.

Stdlib only. It runs in the tests/ci-native.sh preflight as
`python3 tests/test_gate_venv.py`.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `python3 -m venv DIR` makes DIR/bin/python, a stub whose `-m pip` call is
# recorded (and fails when FAIL_PIP is set) and whose `-I -c` runs for real.
FAKE_PYTHON3 = f"""#!/bin/sh
[ "$1 $2" = "-m venv" ] || exit 9
echo "venv $3" >> "$LOG"
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'STUB'
#!/bin/sh
if [ "$1 $2" = "-m pip" ]; then echo "pip $*" >> "$LOG"; [ -z "${{FAIL_PIP:-}}" ]; exit; fi
exec {sys.executable} "$@"
STUB
chmod +x "$3/bin/python"
"""


class GateVenv(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gate-venv-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        (self.base / "bin").mkdir()
        (self.base / "bin/python3").write_text(FAKE_PYTHON3)
        (self.base / "bin/python3").chmod(0o755)
        self.log = self.base / "log"
        self.venv = self.base / "cache/venv-abc"

    def run_it(self, expect=0, **extra):
        environment = {**os.environ, "PATH": f"{self.base / 'bin'}:{os.environ['PATH']}", "LOG": str(self.log), **extra}
        done = subprocess.run(["sh", str(ROOT / "tests/gate-venv.sh"), str(self.venv), str(self.base / "pack")],
                              capture_output=True, text=True, env=environment, timeout=60)
        self.assertEqual(expect, done.returncode, done.stdout + done.stderr)
        return done

    def builds(self):
        return [line for line in self.log.read_text().splitlines() if line.startswith("venv ")] if self.log.exists() else []

    def test_a_second_gate_reuses_the_venv(self):
        first = self.run_it()
        self.assertIn(f"gate venv: built {self.venv}", first.stdout)
        self.assertTrue((self.venv / "bin/python").exists())
        second = self.run_it()
        self.assertIn(f"gate venv: reusing {self.venv}", second.stdout)
        self.assertEqual(1, len(self.builds()), self.builds())
        self.assertIn(f"pip -m pip install -q --no-index {self.base / 'pack'}/lib", self.log.read_text())

    def test_a_failed_install_leaves_no_venv_and_the_next_gate_builds(self):
        self.run_it(expect=1, FAIL_PIP="1")
        self.assertFalse(self.venv.exists())
        self.assertEqual([], [p.name for p in self.venv.parent.iterdir()])
        self.run_it()
        self.assertEqual(2, len(self.builds()))
        self.assertTrue((self.venv / "bin/python").exists())

    def test_a_venv_another_gate_built_meanwhile_stands(self):
        # The fake venv step makes the other gate's venv first.
        racing = FAKE_PYTHON3 + f'mkdir -p "{self.venv}/bin"; cp "$3/bin/python" "{self.venv}/bin/python"; : > "{self.venv}/theirs"\n'
        (self.base / "bin/python3").write_text(racing)
        done = self.run_it()
        self.assertIn("another gate built", done.stdout)
        self.assertTrue((self.venv / "theirs").exists())
        self.assertEqual(["venv-abc"], [p.name for p in self.venv.parent.iterdir()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
