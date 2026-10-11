"""The pinned writing manifest, registered with the pinned pack, imports.

`.github/fixtures/writing-sd-plugin.json` is the writing pack's manifest. This
registers it with the pack at `SD_ACCEPTANCE_PACK` and imports a fixture vault
through its `workflow` block, reading the plugin back through `Reader.at` and
the real `sd plugin list --json`. The manifest is this repository's fixture and
the importer is the pack's, so the check lives here. It runs under the
preflight's virtualenv, which holds the pack's `sd_db`.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.sources import run
from sd_db.sources.vault import SOURCE, Reader

SYSTEM = Path(__file__).resolve().parents[1]
PACK = Path(os.environ.get("SD_ACCEPTANCE_PACK", SYSTEM.parent / "pack"))
WRITING_MANIFEST = SYSTEM / ".github/fixtures/writing-sd-plugin.json"

NOTE = """\
---
contexts:
  - Personal
content-type: {kind}
status: {stage}
dateCreated: 2026-06-15
description: "{title}, at {stage}"
tags:
  - {kind}
---

# {title}

the {kind} body of {title}.
"""


def vault(root: Path, *, ideas: dict[str, str], topics: dict[str, str]) -> Path:
    """A vault with the two bases the migration reads. `{name: stage}`."""
    for base, kind, entries in (("System/Databases/Blog Ideas", "blog-idea", ideas),
                                ("System/Databases/Topics", "topic", topics)):
        (root / base).mkdir(parents=True, exist_ok=True)
        for name, stage in entries.items():
            (root / base / f"{name}.md").write_text(
                NOTE.format(kind=kind, stage=stage, title=name), encoding="utf-8")
    return root


class TheWritingDeclaration(unittest.TestCase):
    def test_the_pinned_pack_accepts_the_declaration_and_both_kinds_land(self):
        self.assertTrue((PACK / "bin/sd").is_file(),
                        "set SD_ACCEPTANCE_PACK to the companion pack checkout")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            checkout = root / "writing"
            checkout.mkdir()
            manifest = json.loads(WRITING_MANIFEST.read_text(encoding="utf-8"))
            (checkout / "sd-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
            for kind in manifest["kinds"].values():
                template = checkout / kind["sections"]["template"]
                template.parent.mkdir(parents=True, exist_ok=True)
                template.touch()
            home = root / "home"
            receipt = home / ".local/state/sd-ai-command-pack/installed.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({"schema": 1, "checkout": str(PACK), "branch": "main",
                                           "commit": "0" * 40, "dirty": False}), encoding="utf-8")
            notes = vault(root / "vault", ideas={"an idea": "inbox"}, topics={"a topic": "candidate"})
            environ = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "config"),
                "OBSIDIAN_VAULT": str(notes),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            added = subprocess.run([sys.executable, str(PACK / "bin/sd"), "plugin", "add", str(checkout)],
                                   capture_output=True, text=True, env=environ, check=False)
            self.assertEqual(added.returncode, 0, added.stderr)
            database = root / "sd.db"
            initialise(database)
            connection = connect(database)
            self.addCleanup(connection.close)
            sitting = run(connection, Reader.at(home=home, environ=environ))
            self.assertTrue(sitting.clean)
            found = {row["external_id"]: row["status"] for row in connection.execute(
                "SELECT external_id, status FROM item WHERE source = ?", (SOURCE,))}
            self.assertEqual(found, {
                "blog-idea:System/Databases/Blog Ideas/an idea.md": "planning",
                "topic:System/Databases/Topics/a topic.md": "planning",
            })


if __name__ == "__main__":
    unittest.main()
