"""ai-songs-backup.sh mirrors WAV, MP3 and MP4 files and nothing else.

Every case runs against temporary folders and an empty config root, so a real
.env on this machine cannot leak in.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ai-songs-backup.sh"


class AiSongsBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.config = root / "config"
        self.source = root / "source"
        self.destination = root / "destination"
        for folder in ("song-one/audio", "song-one/video", "notes"):
            (self.source / folder).mkdir(parents=True)
        (self.destination / "removed").mkdir(parents=True)
        (self.source / "song-one/audio/song.mp3").write_text("audio\n")
        (self.source / "song-one/video/song.MP4").write_text("video\n")
        # Uppercase, because the WAV include is a bracket expression: a
        # lowercase fixture would pass whether or not the case folding is right.
        (self.source / "song-one/audio/master.WAV").write_text("master\n")
        (self.source / "notes/readme.txt").write_text("ignore\n")
        (self.destination / "removed/old.mp3").write_text("delete\n")
        (self.destination / "unrelated.txt").write_text("delete\n")

    def run_script(self, *args, **env):
        base = {k: v for k, v in os.environ.items()
                if k not in ("AI_SONGS_SOURCE", "AI_SONGS_DESTINATION")}
        base["SYSTEM_TOOLS_CONFIG"] = str(self.config)
        base.update(env)
        return subprocess.run(["sh", str(SCRIPT), *args], env=base,
                              capture_output=True, text=True)

    def sync(self, source=None):
        return self.run_script("sync", AI_SONGS_SOURCE=str(source or self.source),
                               AI_SONGS_DESTINATION=str(self.destination))

    def test_help_exits_zero(self):
        result = self.run_script("help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("AI_SONGS_SOURCE", result.stdout)

    def test_no_args_prints_usage_and_fails(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stderr)

    def test_sync_copies_media_in_any_case(self):
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("song-one/audio/song.mp3", "song-one/video/song.MP4",
                     "song-one/audio/master.WAV"):
            self.assertEqual((self.destination / name).read_text(),
                             (self.source / name).read_text(), name)

    def test_sync_deletes_what_is_not_mirrored_media(self):
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("notes/readme.txt", "removed/old.mp3", "unrelated.txt"):
            self.assertFalse((self.destination / name).exists(), name)

    def test_a_source_without_media_is_refused(self):
        self.assertEqual(self.sync().returncode, 0)
        empty = pathlib.Path(self.tmp.name) / "empty-source"
        empty.mkdir()
        (empty / "readme.txt").write_text("not media\n")
        result = self.sync(source=empty)
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to empty destination", result.stderr)
        self.assertTrue((self.destination / "song-one/audio/song.mp3").exists())

    def test_a_missing_variable_names_itself_and_both_remedies(self):
        result = self.run_script("list", AI_SONGS_DESTINATION=str(self.destination))
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"(?s)AI_SONGS_SOURCE.*Export.*\.env\.example")


if __name__ == "__main__":
    unittest.main()
