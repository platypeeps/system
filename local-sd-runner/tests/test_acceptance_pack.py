"""`runner.sh test` names the companion pack on this machine, and only there.

`test_ship_lifecycle` skips without a pack, and its default is CI's layout, a
`pack` beside the checkout, which no machine has. So a local run skipped the
runner's one cross-repository acceptance test and said OK. The entrypoint now
names the pack path its runtime already assumes, when that path holds
`bin/sd-ship` and no value was given. The interpreter is a stub that prints
the variable.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TheEntrypointNamesThePack(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.home)], check=False)
        self.python = self.home / "python"
        self.python.write_text('#!/bin/sh\nprintf "pack=%s\\n" "${SD_ACCEPTANCE_PACK:-}"\n')
        self.python.chmod(0o755)
        self.pack = self.home / "repos/platypeeps/sd-ai-command-pack"

    def seen(self, verb, **extra):
        env = {k: v for k, v in os.environ.items() if k != "SD_ACCEPTANCE_PACK"}
        env.update(HOME=str(self.home), PYTHON=str(self.python), **extra)
        completed = subprocess.run([str(ROOT / "runner.sh"), verb], capture_output=True, text=True,
                                   env=env, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def install_pack(self):
        (self.pack / "bin").mkdir(parents=True)
        # Not executable: the lifecycle test runs `sd-ship` through
        # `sys.executable` and skips only when it is not a file.
        (self.pack / "bin/sd-ship").write_text("#!/bin/sh\n")
        (self.pack / "bin/sd-ship").chmod(0o644)

    def test_the_pack_on_this_machine_is_named_for_test_and_check(self):
        self.install_pack()
        self.assertEqual(self.seen("test"), f"pack={self.pack}")
        self.assertEqual(self.seen("check"), f"pack={self.pack}")

    def test_an_explicit_value_is_never_replaced(self):
        self.install_pack()
        self.assertEqual(self.seen("test", SD_ACCEPTANCE_PACK="/elsewhere"), "pack=/elsewhere")

    def test_no_pack_names_nothing(self):
        self.assertEqual(self.seen("test"), "pack=")
