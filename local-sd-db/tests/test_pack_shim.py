"""`sd-db.sh` runs the pack's `bin/sd-db` for the database verbs (sd:3279).

The pack owns `sd_db` (sd:2997), so the library verbs run its entrypoint
from the pack checkout, `SD_PACK_ROOT`. A fake pack's `bin/sd-db` stands in
here and prints what it was given: the arguments, the interpreter and the
mail path. `SD_DB_LIBRARY=checkout` or `installed` still names this frozen
copy, which its own suite drives until step 4 deletes it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = PACKAGE_ROOT / "sd-db.sh"
NOTIFY_SCRIPT = (PACKAGE_ROOT.parent / "local-notify" / "notify.sh").resolve()

STUB = """\
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "python": sys.executable,
                  "notify": os.environ.get("SD_NOTIFY")}))
"""


class PackShim(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pack = self.root / "pack"
        (self.pack / "bin").mkdir(parents=True)
        (self.pack / "bin/sd-db").write_text(STUB)
        # A PATH with no `notify` on it, and the interpreter by name.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        os.symlink(sys.executable, self.bin / "python3")

    def run_shim(self, *args, **extra):
        environment = {"HOME": str(self.root), "PATH": f"{self.bin}:/usr/bin:/bin",
                       "SD_PACK_ROOT": str(self.pack), "PYTHON": sys.executable, **extra}
        return subprocess.run(["sh", str(ENTRYPOINT), *args], capture_output=True, text=True,
                              input="", env=environment, check=False)

    def test_a_database_verb_runs_the_packs_bin_sd_db_under_the_chosen_python(self):
        done = self.run_shim("repo", "list", "--managed")
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout)
        self.assertEqual(seen["argv"], ["repo", "list", "--managed"])
        self.assertEqual(seen["python"], sys.executable)

    def test_every_library_verb_goes_to_the_pack(self):
        for verb in ("init", "migrate", "status", "restore", "repo", "item", "work", "import",
                     "verify", "usage", "judgments", "credentials", "backup", "serve"):
            with self.subTest(verb=verb):
                done = self.run_shim(verb, "x")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout)["argv"], [verb, "x"])

    def test_with_no_python_named_it_picks_one_new_enough(self):
        # A bare cron PATH puts an old python3 first; sd-db.sh skips it.
        done = self.run_shim("status", PYTHON="", SD_DB_PYTHON=sys.executable)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["python"], sys.executable)

    def test_the_mail_path_is_the_notify_script_when_notify_is_not_on_path(self):
        seen = json.loads(self.run_shim("backup").stdout)
        self.assertEqual(Path(seen["notify"]).resolve(), NOTIFY_SCRIPT)

    def test_notify_on_path_and_an_explicit_sd_notify_are_kept(self):
        (self.bin / "notify").write_text("#!/bin/sh\n")
        (self.bin / "notify").chmod(0o755)
        self.assertEqual(json.loads(self.run_shim("backup").stdout)["notify"], None)
        seen = json.loads(self.run_shim("backup", SD_NOTIFY="/bin/true").stdout)
        self.assertEqual(seen["notify"], "/bin/true")

    def test_a_pack_without_bin_sd_db_refuses_and_names_the_remedy(self):
        shutil.rmtree(self.pack / "bin")
        done = self.run_shim("status")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn(f"{self.pack}/bin/sd-db", done.stderr)
        self.assertIn("repo-sync.sh refresh", done.stderr)
        self.assertIn("repo-sync.sh follow", done.stderr)

    def test_help_needs_no_pack(self):
        shutil.rmtree(self.pack)
        done = self.run_shim("help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("SD_PACK_ROOT", done.stderr)


if __name__ == "__main__":
    unittest.main()
