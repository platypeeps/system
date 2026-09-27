"""`sd-db.sh test` finds the companion pack on this machine, and only there.

`test_scheduled_recovery` fails without a pack, because a skipped test fails
CI, and its default is CI's layout: a `pack` beside the checkout. On a
machine that layout does not exist, so a bare local `sd-db.sh test` failed
one test and read as a regression. The entrypoint now names the pack path
its Python candidates already assume, when that path holds `bin/sd` and no
value was given.

The interpreter is a stub that prints the variable, so these cases see what
the suite would have seen without running it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parent.parent / "sd-db.sh"


class TheEntrypointNamesThePack(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.home)], check=False)
        self.python = self.home / "python"
        # `-c` is the entrypoint's version probe, which must pass and say
        # nothing; the run that follows is the one that prints.
        self.python.write_text('#!/bin/sh\n[ "$1" = -c ] && exit 0\n'
                               'printf "pack=%s\\n" "${SD_ACCEPTANCE_PACK:-}"\n')
        self.python.chmod(0o755)
        self.pack = self.home / "repos/platypeeps/sd-ai-command-pack"

    def seen(self, verb, **extra):
        env = {k: v for k, v in os.environ.items() if k != "SD_ACCEPTANCE_PACK"}
        env.update(HOME=str(self.home), PYTHON=str(self.python), **extra)
        completed = subprocess.run([str(ENTRYPOINT), verb], capture_output=True, text=True,
                                   env=env, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def install_pack(self):
        (self.pack / "bin").mkdir(parents=True)
        # Not executable: the test asks only that `bin/sd` is a file, as CI's
        # `test -f` does, so the entrypoint asks no more.
        (self.pack / "bin/sd").write_text("#!/bin/sh\n")
        (self.pack / "bin/sd").chmod(0o644)

    def test_the_pack_on_this_machine_is_named_for_test_and_check(self):
        self.install_pack()
        self.assertEqual(self.seen("test"), f"pack={self.pack}")
        self.assertEqual(self.seen("check"), f"pack={self.pack}")

    def test_an_explicit_value_is_never_replaced(self):
        self.install_pack()
        self.assertEqual(self.seen("test", SD_ACCEPTANCE_PACK="/elsewhere"), "pack=/elsewhere")

    def test_no_pack_names_nothing_so_the_test_still_fails_in_its_own_sentence(self):
        self.assertEqual(self.seen("test"), "pack=")

    def test_a_folder_without_bin_sd_is_not_a_pack(self):
        self.pack.mkdir(parents=True)
        self.assertEqual(self.seen("test"), "pack=")


if __name__ == "__main__":
    unittest.main()
