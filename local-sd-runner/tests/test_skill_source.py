"""Verify catalog bindings before any reviewer or author can run."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from sd_db.errors import SdDbError
from sd_db.skills_catalog import MAX_TEXT, _hash, _snapshot
from sd_runner.runtime import verify_skill_source


class SkillSource(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "skills/sd-example"
        (self.directory / "references").mkdir(parents=True)
        self.selected = self.directory / "SKILL.md"
        self.selected.write_text("Fixture skill.\n")
        self.reference = self.directory / "references/guide.md"
        self.reference.write_text("Bound reference.\n")
        self.manifest = self.root / "skills/paths.json"
        self.manifest.write_text('{"paths": {}}\n')

    def request(self, action="review"):
        files = _snapshot(self.root, self.directory)
        if action in {"promote", "demote"}:
            files["skills/paths.json"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        source = {"path": "skills/sd-example/SKILL.md", "files": files,
                  "source_sha256": _hash(files), "action": action}
        return {"item_record": {"fields": json.dumps({"skill_review": source})},
                "run": {"work_path": str(self.root)}}

    def test_unchanged_review_and_apply_include_all_skill_files(self):
        for action in ("review", "apply"):
            with self.subTest(action=action):
                verify_skill_source(self.request(action))

    def test_unchanged_move_includes_explicit_path_manifest(self):
        for action in ("promote", "demote"):
            with self.subTest(action=action):
                verify_skill_source(self.request(action))

    def test_modified_skill_file_is_refused(self):
        request = self.request()
        self.reference.write_text("Changed reference.\n")
        with self.assertRaisesRegex(SdDbError, "skill source changed"):
            verify_skill_source(request)

    def test_added_skill_file_is_refused(self):
        request = self.request()
        (self.directory / "new.md").write_text("New instruction.\n")
        with self.assertRaisesRegex(SdDbError, "skill source changed"):
            verify_skill_source(request)

    def test_removed_skill_file_is_refused(self):
        request = self.request()
        self.reference.unlink()
        with self.assertRaisesRegex(SdDbError, "skill source changed"):
            verify_skill_source(request)

    def test_changed_path_manifest_is_refused_for_each_move(self):
        requests = [self.request(action) for action in ("promote", "demote")]
        self.manifest.write_text('{"paths": {"changed": {}}}\n')
        for request in requests:
            with self.subTest(fields=request["item_record"]["fields"]):
                with self.assertRaisesRegex(SdDbError, "skill source changed"):
                    verify_skill_source(request)

    def test_unrelated_repository_file_does_not_change_review_binding(self):
        request = self.request()
        self.manifest.write_text('{"unrelated": true}\n')
        (self.root / "README.md").write_text("Unrelated repository file.\n")
        verify_skill_source(request)

    def test_invalid_and_outside_selected_paths_are_refused(self):
        for named in (None, "", "skills/sd-example", "../SKILL.md", str(self.selected)):
            with self.subTest(path=named):
                request = self.request()
                fields = json.loads(request["item_record"]["fields"])
                fields["skill_review"]["path"] = named
                request["item_record"]["fields"] = json.dumps(fields)
                with self.assertRaises(SdDbError):
                    verify_skill_source(request)

    def test_linked_selected_file_is_refused(self):
        request = self.request()
        target = self.root / "linked-target"
        target.write_bytes(self.selected.read_bytes())
        self.selected.unlink()
        self.selected.symlink_to(target)
        with self.assertRaisesRegex(SdDbError, "linked"):
            verify_skill_source(request)

    def test_linked_selected_directory_is_refused(self):
        request = self.request()
        target = self.root / "linked-directory"
        self.directory.rename(target)
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(SdDbError, "linked"):
            verify_skill_source(request)

    def test_linked_reference_is_refused(self):
        request = self.request()
        target = self.root / "linked-reference"
        target.write_bytes(self.reference.read_bytes())
        self.reference.unlink()
        self.reference.symlink_to(target)
        with self.assertRaisesRegex(SdDbError, "linked"):
            verify_skill_source(request)

    def test_oversized_reference_is_refused(self):
        request = self.request()
        self.reference.write_bytes(b"x" * (MAX_TEXT + 1))
        with self.assertRaisesRegex(SdDbError, "oversized"):
            verify_skill_source(request)

    def test_missing_path_manifest_is_refused(self):
        request = self.request("promote")
        self.manifest.unlink()
        with self.assertRaisesRegex(SdDbError, "invalid"):
            verify_skill_source(request)

    def test_linked_path_manifest_is_refused(self):
        request = self.request("demote")
        target = self.root / "linked-paths"
        target.write_bytes(self.manifest.read_bytes())
        self.manifest.unlink()
        self.manifest.symlink_to(target)
        with self.assertRaisesRegex(SdDbError, "linked"):
            verify_skill_source(request)

    def test_oversized_path_manifest_is_refused(self):
        request = self.request("promote")
        self.manifest.write_bytes(b"x" * (MAX_TEXT + 1))
        with self.assertRaisesRegex(SdDbError, "oversized"):
            verify_skill_source(request)
