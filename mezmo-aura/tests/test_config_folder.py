"""Where `aura.sh` reads its private config: `<config>/mezmo-aura/` (sd:2539).

Convention 3 keeps a `mezmo-*` folder's full name, so the folder rename moved
the config folder from `<config>/aura/` with it. A machine that still has only
the old folder is refused with both paths named, and nothing is moved or read
from it: the operator moves their own files. `help` still answers, because
the health-check sweep probes every entrypoint's help.

`aura` is a stub on PATH that prints its arguments, so no Aura runs and no
key is needed beyond the fixture's own.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "aura.sh"

STUB = """#!/bin/sh
echo "aura $*"
"""


class ConfigFolder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "bin").mkdir()
        stub = root / "bin" / "aura"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.config = root / "config"
        self.new = self.config / "mezmo-aura"
        self.old = self.config / "aura"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OTEL_", "AURA_", "GENAI_TRACES_", "LLM_"))
                    and k not in ("OPENAI_API_KEY", "MEZMO_API_KEY", "CONFIG_PATH")}
        self.env.update(PATH=f"{root / 'bin'}:{os.environ['PATH']}", SYSTEM_TOOLS_CONFIG=str(self.config),
                        AURA_TRACES="0")

    def aura(self, *args):
        return subprocess.run(["/bin/sh", str(ENTRYPOINT), *args], capture_output=True, text=True,
                              env=self.env, timeout=30)

    def keys(self, folder):
        folder.mkdir(parents=True)
        (folder / ".env").write_text("OPENAI_API_KEY=k\nMEZMO_API_KEY=k\n", encoding="utf-8")

    def test_the_env_file_and_config_toml_are_read_from_the_mezmo_aura_folder(self):
        self.keys(self.new)
        (self.new / "config.toml").write_text("# operator's own\n", encoding="utf-8")
        done = self.aura("server")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"--config {self.new / 'config.toml'}", done.stdout)

    def test_a_missing_key_names_the_mezmo_aura_folder_and_example(self):
        done = self.aura("server")
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"copy mezmo-aura/.env.example to {self.new}/.env", done.stderr)

    def test_only_the_old_folder_is_refused_naming_both_paths_and_left_alone(self):
        self.keys(self.old)
        done = self.aura("server")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertEqual(done.stdout, "", "the server started from the old folder")
        self.assertIn(str(self.old), done.stderr)
        self.assertIn(str(self.new), done.stderr)
        self.assertIn(f"mv {self.old} {self.new}", done.stderr)
        self.assertTrue((self.old / ".env").is_file(), "the old folder was moved or changed")
        self.assertFalse(self.new.exists(), "the new folder was created for the operator")

    def test_both_folders_read_the_new_one_and_name_the_old_one(self):
        self.keys(self.new)
        self.old.mkdir()
        (self.old / ".env").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
        done = self.aura("server")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"{self.old} is no longer read", done.stderr)

    def test_help_answers_with_only_the_old_folder(self):
        self.keys(self.old)
        done = self.aura("help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("<config>/mezmo-aura/.env", done.stdout)


if __name__ == "__main__":
    unittest.main()
