"""`stubs.executable`: a stand-in behaves as the file it replaces would."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from stubs import executable


class Executable(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stubs-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_it(self, command, **more):
        return subprocess.run(command, capture_output=True, text=True, timeout=30, **more)

    def test_a_shell_stand_in_keeps_its_path_arguments_and_exit(self):
        stand_in = executable(self.root / "tool", '#!/bin/sh\necho "$0|$*"\nexit 7\n')
        result = self.run_it([str(stand_in), "one", "two words"])
        self.assertEqual(result.stdout, f"{stand_in}|one two words\n")
        self.assertEqual(result.returncode, 7)

    def test_a_stand_in_is_found_on_path_by_its_name(self):
        executable(self.root / "git", "#!/bin/sh\necho stand-in\n")
        result = self.run_it(["git"], env={"PATH": f"{self.root}:/usr/bin:/bin"})
        self.assertEqual(result.stdout, "stand-in\n")

    def test_exec_replaces_the_stand_ins_own_process(self):
        # A deadline test kills the pid the stand-in recorded; that pid has to
        # be the process `exec` leaves, not a shell above it.
        stand_in = executable(self.root / "tool", f'#!/bin/sh\necho $$\nexec "{sys.executable}" -c "import os; print(os.getpid())"\n')
        shell, replaced = self.run_it([str(stand_in)]).stdout.split()
        self.assertEqual(shell, replaced)

    def test_another_interpreter_gets_the_arguments_after_its_source(self):
        stand_in = executable(self.root / "tool", f"#!{sys.executable}\nimport sys\nprint(sys.argv[1:])\n")
        self.assertEqual(self.run_it([str(stand_in), "a", "b"]).stdout, "['a', 'b']\n")

    def test_writing_a_stand_in_again_replaces_it(self):
        executable(self.root / "tool", "#!/bin/sh\necho first\n")
        stand_in = executable(self.root / "tool", "#!/bin/sh\necho second\n")
        self.assertEqual(self.run_it([str(stand_in)]).stdout, "second\n")

    def test_text_without_an_interpreter_line_is_refused(self):
        with self.assertRaises(ValueError):
            executable(self.root / "tool", "echo no interpreter\n")
        self.assertFalse(os.path.lexists(self.root / "tool"))


if __name__ == "__main__":
    unittest.main()
