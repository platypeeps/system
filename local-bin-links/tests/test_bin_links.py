"""bin-links.sh no longer links the sd-ai-command-pack's commands, and sweeps the old links.

The pack's own installer links its commands into ~/.local/bin. A link in the
bin dir that this script made earlier shadows that install, and a link whose
command the pack dropped dangles. So any symlink whose target is inside the
pack checkout is reported STALE by `status` and removed by `install` and
`remove`. A regular file, or a link that points elsewhere, is never touched.

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

    def old_links(self, *names):
        """Links as an earlier bin-links made them: one per pack command."""
        self.bin.mkdir(exist_ok=True)
        for name in names:
            (self.bin / name).symlink_to(self.pack / "bin" / name)

    def test_install_links_no_pack_command(self):
        out = self.run_script("install")
        self.assertEqual(self.pack_links(), [])
        self.assertNotIn("sd-status", out)
        self.assertNotIn("sd-ai-command-pack not found", out)

    def test_install_removes_an_old_pack_link_and_prints_it(self):
        self.old_links("sd", "sd-status")
        out = self.run_script("install")
        self.assertEqual(self.pack_links(), [])
        self.assertFalse(os.path.lexists(self.bin / "sd"))
        self.assertIn("removed", self.line(out, "sd-status"))

    def test_install_removes_a_dangling_pack_link(self):
        self.old_links("sd-review")
        (self.pack / "bin/sd-review").unlink()
        self.run_script("install")
        self.assertFalse(os.path.lexists(self.bin / "sd-review"))

    def test_status_names_an_old_pack_link_stale(self):
        self.old_links("sd-status")
        self.assertIn("STALE", self.line(self.run_script("status"), "sd-status"))
        self.assertTrue(os.path.islink(self.bin / "sd-status"))

    def test_a_real_file_with_a_pack_name_is_never_removed(self):
        self.bin.mkdir()
        (self.bin / "sd").write_text("#!/bin/sh\n")
        self.run_script("install")
        self.assertEqual((self.bin / "sd").read_text(), "#!/bin/sh\n")
        self.assertNotIn("sd", self.run_script("status").split())

    def test_a_link_elsewhere_is_not_ours(self):
        self.bin.mkdir()
        elsewhere = pathlib.Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "sd-live").write_text("#!/bin/sh\n")
        (self.bin / "sd-live").symlink_to(elsewhere / "sd-live")
        (self.bin / "sd-foreign").symlink_to(elsewhere / "sd-foreign")
        self.run_script("install")
        self.assertTrue(os.path.lexists(self.bin / "sd-live"))
        self.assertTrue(os.path.lexists(self.bin / "sd-foreign"))

    def test_remove_takes_old_pack_links(self):
        self.old_links("sd", "sd-review")
        self.run_script("remove")
        self.assertEqual(self.pack_links(), [])

    def test_repo_rows_are_still_linked(self):
        out = self.run_script("install")
        self.assertIn("repo-sync", out)
        self.assertTrue((self.bin / "repo-sync").is_symlink())


if __name__ == "__main__":
    unittest.main()
