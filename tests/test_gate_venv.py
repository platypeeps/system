"""`tests/gate-venv.sh`: the gate's venv, built once per key (sd:9926).

tests/check.sh names the venv by the pack's lib/ tree and the python; the
preflight and each macOS-only suite call gate-venv.sh, which reuses a venv
that exists and otherwise builds one beside it and renames it in whole.
A fake python3 records each build, so this runs in a second.

Stdlib only. It runs in the tests/ci-native.sh preflight as
`python3 tests/test_gate_venv.py`.
"""

import fcntl
import os
import shutil
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

    def test_the_venv_is_built_by_the_real_python_not_a_link_to_it(self):
        # sd:9926 review: check.sh puts python3 on PATH as a link in the
        # gate's own worktree, and venv points bin/python at the path it
        # ran as: once that worktree went, the cached venv's python
        # dangled. The venv is built by the link's real target.
        real = self.base / "real"
        real.mkdir()
        (self.base / "bin/python3").rename(real / "python3")
        (self.base / "bin/python3").symlink_to(real / "python3")
        (real / "python3").write_text(FAKE_PYTHON3.replace('echo "venv $3"', 'echo "venv $3 by $0"'))
        self.run_it()
        self.assertEqual([f"venv {self.venv}.build.", f"by {os.path.realpath(real / 'python3')}"],
                         [self.builds()[0][:len(f"venv {self.venv}.build.")], self.builds()[0].split(" ", 2)[2]])

    def test_a_venv_whose_python_dangles_is_built_again(self):
        # sd:9926 review: a venv whose python link dangled read as a
        # miss, the new one could not be renamed onto the old directory,
        # and every gate with that key failed until a hand cleanup.
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin/python").symlink_to(self.base / "gone/python3")
        done = self.run_it()
        self.assertIn(f"gate venv: built {self.venv}, in place of a broken one", done.stdout)
        self.assertTrue(os.access(self.venv / "bin/python", os.X_OK))
        self.assertEqual(["venv-abc"], [p.name for p in self.venv.parent.iterdir()])

    def test_a_repair_keeps_the_venv_another_gate_repaired_meanwhile(self):
        # sd:9926 review: two gates found the same broken venv; the
        # second removed the one the first had just put in place, and
        # the first gate's suites lost their python. Here the test is
        # the other gate: it holds the cache folder's lock, repairs the
        # venv, then lets go; the gate must keep that venv.
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin/python").symlink_to(self.base / "gone/python3")
        lock = os.open(self.venv.parent, os.O_RDONLY)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        environment = {**os.environ, "PATH": f"{self.base / 'bin'}:{os.environ['PATH']}", "LOG": str(self.log)}
        gate = subprocess.Popen(["sh", str(ROOT / "tests/gate-venv.sh"), str(self.venv), str(self.base / "pack")],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment)
        try:
            gate.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(self.venv)
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin/python").write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
        (self.venv / "bin/python").chmod(0o755)
        (self.venv / "theirs").touch()
        fcntl.flock(lock, fcntl.LOCK_UN)
        output, _ = gate.communicate(timeout=60)
        self.assertEqual(0, gate.returncode, output)
        self.assertIn("another gate built", output)
        self.assertTrue((self.venv / "theirs").exists())
        self.assertEqual(["venv-abc"], [p.name for p in self.venv.parent.iterdir()])

    def test_a_miss_publishes_under_the_lock_too(self):
        # sd:9926 lane review: a miss renamed its venv in without the
        # lock. A repair killed after it moved the broken venv aside left
        # the path missing; a fresh gate published there while a queued
        # repair went on, and that repair moved the healthy venv away.
        # Here the test is the other publisher: it holds the lock, so the
        # gate must wait for it, then keep the venv it put in place.
        self.venv.parent.mkdir(parents=True)
        lock = os.open(self.venv.parent, os.O_RDONLY)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        environment = {**os.environ, "PATH": f"{self.base / 'bin'}:{os.environ['PATH']}", "LOG": str(self.log)}
        gate = subprocess.Popen(["sh", str(ROOT / "tests/gate-venv.sh"), str(self.venv), str(self.base / "pack")],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment)
        try:
            gate.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self.assertIsNone(gate.poll(), "the gate published while another held the lock")
        self.assertFalse(self.venv.exists())
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin/python").write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
        (self.venv / "bin/python").chmod(0o755)
        (self.venv / "theirs").touch()
        fcntl.flock(lock, fcntl.LOCK_UN)
        output, _ = gate.communicate(timeout=60)
        self.assertEqual(0, gate.returncode, output)
        self.assertIn("another gate built", output)
        self.assertTrue((self.venv / "theirs").exists())
        self.assertEqual(["venv-abc"], [p.name for p in self.venv.parent.iterdir()])

    def test_a_venv_a_killed_repair_moved_aside_is_removed(self):
        # A repair killed between moving the broken venv aside and
        # removing it leaves VENV.broken.PID; the next publisher, under
        # the lock, removes it and builds VENV.
        aside = self.venv.parent / "venv-abc.broken.12345"
        (aside / "bin").mkdir(parents=True)
        (aside / "bin/python").symlink_to(self.base / "gone/python3")
        done = self.run_it()
        self.assertIn(f"gate venv: built {self.venv}", done.stdout)
        self.assertTrue(os.access(self.venv / "bin/python", os.X_OK))
        self.assertEqual(["venv-abc"], [p.name for p in self.venv.parent.iterdir()])

if __name__ == "__main__":
    unittest.main(verbosity=2)
