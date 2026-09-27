"""bin-links.sh links every command the sd-ai-command-pack checkout ships.

The pack's installer renders surfaces and links no executable; its commands are
invoked by path, and PATH is this repository's job. They used to be two
hand-written rows, `sd` and `sd-research-kit`, while the pack shipped
seventeen. So the pack's commands are read from its `bin/` at run time, by the
rule the pack's own `sd_install.py --status` counts them with: `sd*`,
extensionless, a regular executable file. The `sd_*.py` beside them are
modules.

A command the pack stops shipping leaves a link to nothing behind, and nothing
names it any more, so a link into the pack's `bin/` whose target is gone is
reported STALE by `status` and removed by `install` and `remove`.

Fixtures: a fake pack checkout and a scratch bin dir, both through the
environment the script already reads.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = FOLDER / "bin-links.sh"


class BinLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.pack = base / "pack"
        self.bin = base / "bin-common"
        (self.pack / "bin").mkdir(parents=True)
        for name in ("sd", "sd-status", "sd-review"):
            self.command(name)
        # Not commands: a module, an executable with an extension, a
        # non-executable file, a directory, and something not named sd.
        (self.pack / "bin/sd_lib.py").write_text("")
        self.command("sd-helper.sh")
        (self.pack / "bin/sd-notes").write_text("#!/bin/sh\n")
        (self.pack / "bin/sd-dir").mkdir()
        self.command("other-tool")

    def command(self, name):
        path = self.pack / "bin" / name
        path.write_text("#!/bin/sh\necho ok\n")
        path.chmod(0o755)
        return path

    def run_script(self, verb):
        env = dict(os.environ, BIN_LINKS_DIR=str(self.bin), SD_PACK_ROOT=str(self.pack))
        result = subprocess.run([str(SCRIPT), verb], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def pack_links(self):
        return sorted(p.name for p in self.bin.iterdir()
                      if p.is_symlink() and os.readlink(p).startswith(str(self.pack)))

    def line(self, out, name):
        return next(line for line in out.splitlines() if line.split()[:1] == [name])

    def test_install_links_every_pack_command_and_nothing_else(self):
        self.run_script("install")
        self.assertEqual(self.pack_links(), ["sd", "sd-review", "sd-status"])
        self.assertEqual(os.readlink(self.bin / "sd-status"), str(self.pack / "bin/sd-status"))

    def test_status_names_an_unlinked_pack_command_missing(self):
        self.run_script("install")
        self.command("sd-new")
        self.assertIn("MISSING", self.line(self.run_script("status"), "sd-new"))
        self.run_script("install")
        self.assertIn("linked", self.line(self.run_script("status"), "sd-new"))

    def test_a_command_the_pack_dropped_is_stale_then_swept(self):
        self.run_script("install")
        (self.pack / "bin/sd-review").unlink()
        self.assertIn("STALE", self.line(self.run_script("status"), "sd-review"))
        self.run_script("install")
        self.assertFalse(os.path.lexists(self.bin / "sd-review"))
        self.assertNotIn("sd-review", self.run_script("status"))

    def test_a_dangling_link_elsewhere_is_not_ours(self):
        self.bin.mkdir()
        (self.bin / "sd-foreign").symlink_to(pathlib.Path(self.tmp.name) / "elsewhere/sd-foreign")
        self.run_script("install")
        self.assertTrue(os.path.lexists(self.bin / "sd-foreign"))

    def test_remove_takes_pack_links_and_stale_ones(self):
        self.run_script("install")
        (self.pack / "bin/sd-review").unlink()
        self.run_script("remove")
        self.assertEqual(self.pack_links(), [])

    def test_repo_rows_are_still_linked(self):
        out = self.run_script("install")
        self.assertIn("repo-sync", out)
        self.assertTrue((self.bin / "repo-sync").is_symlink())


if __name__ == "__main__":
    unittest.main()
