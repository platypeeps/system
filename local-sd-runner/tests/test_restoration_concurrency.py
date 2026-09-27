"""Different retained sources must not share a destination restore intent."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db.runner import RunnerRefused
from sd_runner import restoration, storage


class RestorationConcurrency(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runs = []
        for ident, content in (("a", b"first source"), ("b", b"second source")):
            clone = self.root / ident / "clone"
            clone.mkdir(parents=True)
            (clone / "value").write_bytes(content)
            self.runs.append({"id": ident * 32, "retained_path": str(clone)})
        self.destination = self.root / "restored"

    def receipt(self):
        token = hashlib.sha256(str(self.destination).encode()).hexdigest()[:24]
        return self.root / f".sd-restore-{token}.json"

    def assert_competing_source_is_refused(self, competing_destination):
        actual_write = restoration._write
        attempts = []

        def write_with_competing_restore(path, value):
            if value["run"] == self.runs[0]["id"]:
                attempts.append(competing_destination)
                with self.assertRaisesRegex(RunnerRefused, "lock held"):
                    storage.restore(self.runs[1], competing_destination)
            actual_write(path, value)

        with patch.object(restoration, "_write", side_effect=write_with_competing_restore):
            self.assertEqual(storage.restore(self.runs[0], self.destination), self.destination)
        self.assertEqual(attempts, [competing_destination])
        before = self.receipt().read_bytes()
        self.assertEqual(json.loads(before)["run"], self.runs[0]["id"])
        self.assertEqual((self.destination / "value").read_bytes(), b"first source")
        self.assertEqual(restoration.status(self.runs[0], self.destination)["state"], "complete")
        with self.assertRaisesRegex(RunnerRefused, "different recorded run"):
            storage.restore(self.runs[1], self.destination)
        self.assertEqual(self.receipt().read_bytes(), before)
        self.assertEqual((self.destination / "value").read_bytes(), b"first source")
        self.assertEqual(storage.restore(self.runs[0], self.destination), self.destination)
        self.assertEqual(self.receipt().read_bytes(), before)

    def test_competing_sources_preserve_successful_restore_and_receipt(self):
        self.assert_competing_source_is_refused(self.destination)

    def test_parent_alias_uses_the_same_destination_lock(self):
        alias = self.root / "parent-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assert_competing_source_is_refused(alias / self.destination.name)

    def test_unrelated_destination_can_restore_while_first_is_in_progress(self):
        actual_write = restoration._write
        other = self.root / "other"

        def write_with_unrelated_restore(path, value):
            if value["run"] == self.runs[0]["id"]:
                self.assertEqual(storage.restore(self.runs[1], other), other)
            actual_write(path, value)

        with patch.object(restoration, "_write", side_effect=write_with_unrelated_restore):
            storage.restore(self.runs[0], self.destination)
        self.assertEqual(restoration.status(self.runs[0], self.destination)["state"], "complete")
        self.assertEqual(restoration.status(self.runs[1], other)["state"], "complete")
        self.assertEqual((other / "value").read_bytes(), b"second source")


if __name__ == "__main__":
    unittest.main()
