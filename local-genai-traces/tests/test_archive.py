"""`archive` keeps the raw traces for good (system sd:5775).

The collector's file exporter rotates storage/raw/traces.jsonl into
traces-<UTC time>.jsonl backups and deletes the oldest past max_backups.
`archive` moves each backup, never the live file, into
GENAI_TRACES_ARCHIVE_DIR as <name>.gz, checks the copy against the source
before removing it, and deletes nothing in the archive. Unset or missing,
it exits 3 (nothing to do); a failed write exits 1. Same stubs as
test_status.py.
"""

import gzip
import re
import unittest

from .test_status import FOLDER, StatusCase

LIVE = "traces.jsonl"
ROTATED = ("traces-2026-10-20T01-02-03.456.jsonl", "traces-2026-10-21T04-05-06.789.jsonl")
# Rotated files waiting (about 2 GB) at which archive and status page.
ALARM = 20


class Archive(StatusCase):
    def setUp(self):
        super().setUp()
        self.raw = self.folder / "storage" / "raw"
        self.raw.mkdir(parents=True)
        self.contents = {}
        for name in (LIVE, *ROTATED):
            body = f'{{"resourceSpans":[{{"file":"{name}"}}]}}\n'.encode() * 50
            (self.raw / name).write_bytes(body)
            self.contents[name] = body
        self.archive = self.root / "share" / "genai-traces"
        self.archive.mkdir(parents=True)

    def configure(self, path):
        folder = self.config / "genai-traces"
        folder.mkdir(exist_ok=True)
        (folder / ".env").write_text(f"GENAI_TRACES_ARCHIVE_DIR={path}\n", encoding="utf-8")

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(self.archive.iterdir())}

    def test_rotated_files_move_gzipped_and_the_live_file_stays(self):
        self.configure(self.archive)
        self.run_verb("archive", expect=0)
        self.assertEqual(sorted(p.name for p in self.archive.iterdir()),
                         [name + ".gz" for name in ROTATED])
        for name in ROTATED:
            self.assertEqual(gzip.decompress((self.archive / (name + ".gz")).read_bytes()),
                             self.contents[name])
            self.assertFalse((self.raw / name).exists())
        self.assertEqual((self.raw / LIVE).read_bytes(), self.contents[LIVE])

    def test_a_second_run_moves_nothing(self):
        self.configure(self.archive)
        self.run_verb("archive", expect=0)
        before = self.snapshot()
        self.run_verb("archive", expect=0)
        self.assertEqual(self.snapshot(), before)

    def test_unset_says_so_and_exits_3(self):
        done = self.run_verb("archive", expect=3)
        self.assertIn("GENAI_TRACES_ARCHIVE_DIR", done.stdout)
        for name in ROTATED:
            self.assertTrue((self.raw / name).exists())

    def test_a_missing_folder_skips_and_is_not_created(self):
        # An unmounted share reads as a missing folder; creating it would put
        # the archive on the system disk under /Volumes.
        gone = self.root / "unmounted" / "genai-traces"
        self.configure(gone)
        done = self.run_verb("archive", expect=3)
        self.assertEqual(len(done.stdout.strip().splitlines()), 1, done.stdout)
        self.assertFalse(gone.parent.exists())
        for name in ROTATED:
            self.assertTrue((self.raw / name).exists())

    def test_a_missing_folder_with_a_backlog_fails_on_disk_growth(self):
        # A share gone for weeks: files pile up on the system disk.
        for n in range(ALARM):
            (self.raw / f"traces-2026-09-01T00-00-{n:02d}.000.jsonl").write_bytes(b"x\n")
        self.configure(self.root / "unmounted" / "genai-traces")
        done = self.run_verb("archive", expect=1)
        self.assertIn("disk", done.stdout)

    def test_every_nas_step_runs_bounded(self):
        # Codex review: the existence checks and the rename ran unbounded, so
        # a share that stops answering hung the run. st_bounded runs each in
        # a process group of its own; these stubs record whether they were.
        stubs = self.root / "bin"
        log = self.root / "bounded.log"
        for name, real in (("mv", "/bin/mv"), ("test", "/bin/test")):
            (stubs / name).write_text(
                '#!/bin/sh\n'
                f'[ "$(ps -o pgid= -p $$ | tr -d " ")" = "$$" ] && b=bounded || b=unbounded\n'
                f'echo "{name} $b" >> "{log}"\nexec {real} "$@"\n', encoding="utf-8")
            (stubs / name).chmod(0o755)
        self.configure(self.archive)
        self.run_verb("archive", expect=0)
        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertIn("mv bounded", calls)
        self.assertIn("test bounded", calls)
        self.assertNotIn("mv unbounded", calls)
        self.assertNotIn("test unbounded", calls)
        # One check of the folder, one of each archive name.
        self.assertEqual(calls.count("test bounded"), 1 + len(ROTATED), calls)

    def test_nothing_in_the_archive_is_deleted_or_overwritten(self):
        older = self.archive / "traces-2026-01-01T00-00-00.000.jsonl.gz"
        older.write_bytes(gzip.compress(b"older\n"))
        notes = self.archive / "notes.txt"
        notes.write_text("keep\n", encoding="utf-8")
        # Same name, other content: the source stays and the run fails.
        clash = self.archive / (ROTATED[0] + ".gz")
        clash.write_bytes(gzip.compress(b"another run\n"))
        kept = {p.name: p.read_bytes() for p in (older, notes, clash)}
        self.configure(self.archive)
        self.run_verb("archive", expect=1)
        for name, body in kept.items():
            self.assertEqual((self.archive / name).read_bytes(), body)
        self.assertTrue((self.raw / ROTATED[0]).exists())
        self.assertFalse((self.raw / ROTATED[1]).exists())

    def test_a_run_stopped_after_the_move_finishes_on_the_next(self):
        # The copy reached the archive; the source was not yet removed.
        self.configure(self.archive)
        copy = self.archive / (ROTATED[0] + ".gz")
        copy.write_bytes(gzip.compress(self.contents[ROTATED[0]]))
        before = copy.stat().st_mtime_ns
        self.run_verb("archive", expect=0)
        self.assertFalse((self.raw / ROTATED[0]).exists())
        self.assertEqual(copy.stat().st_mtime_ns, before)

    def test_a_failed_write_on_a_mounted_share_exits_1_and_keeps_the_source(self):
        self.configure(self.archive)
        self.archive.chmod(0o555)
        self.addCleanup(self.archive.chmod, 0o755)
        self.run_verb("archive", expect=1)
        for name in ROTATED:
            self.assertEqual((self.raw / name).read_bytes(), self.contents[name])

    def test_a_copy_that_does_not_match_is_not_kept_as_archived(self):
        # gzip "succeeds" but writes something else: the check catches it.
        stubs = self.root / "bin"
        (stubs / "gzip").write_text(
            '#!/bin/sh\ncase "$1" in -c) printf "not it" | /usr/bin/gzip -c ;; '
            '*) exec /usr/bin/gzip "$@" ;; esac\n', encoding="utf-8")
        (stubs / "gzip").chmod(0o755)
        self.configure(self.archive)
        self.run_verb("archive", expect=1)
        self.assertEqual([p for p in self.archive.iterdir() if not p.name.startswith(".")], [])
        for name in ROTATED:
            self.assertTrue((self.raw / name).exists())


class DiskGrowth(Archive):
    """Round 3 ruling: archive owns deletion (sd:5775).

    The collector keeps every rotated file (max_backups: 0), so nothing is
    lost between runs; files that are not archived pile up on the system
    disk instead. Twenty waiting (about 2 GB) is a fault archive and status
    report, whatever stops the archive.
    """

    def fill(self, count):
        for name in ROTATED:
            (self.raw / name).unlink()
        for n in range(count):
            (self.raw / f"traces-2026-09-01T00-{n // 60:02d}-{n % 60:02d}.000-size.jsonl"
             ).write_bytes(b"x\n")

    def test_a_large_backlog_is_archived_and_passes(self):
        # No gap to detect: a run after a burst moves it all and succeeds.
        self.fill(40)
        self.configure(self.archive)
        self.run_verb("archive", expect=0)
        self.assertEqual(len(list(self.archive.glob("*.gz"))), 40)

    def test_unset_with_a_backlog_fails_on_disk_growth(self):
        self.fill(ALARM)
        done = self.run_verb("archive", expect=1)
        self.assertIn("GENAI_TRACES_ARCHIVE_DIR", done.stdout)
        self.assertIn("disk", done.stdout)

    def test_unset_short_of_the_alarm_skips(self):
        self.fill(ALARM - 1)
        self.run_verb("archive", expect=3)

    def test_status_fails_on_a_backlog(self):
        # The job not running, or failing: the nightly health check sees it.
        self.serve("GENAI_TRACES_HEALTH_PORT", 200)
        self.serve("GENAI_TRACES_UI_PORT", 200)
        self.run_verb("start", expect=0)
        self.run_verb("status", expect=0)
        self.fill(ALARM)
        done = self.run_verb("status", expect=1)
        self.assertIn("disk", done.stdout)

    def test_status_fails_on_a_backlog_after_stop(self):
        # Codex review: a stopped service answered 3 before the backlog
        # check, so the nightly health check never saw the files on disk.
        self.run_verb("start", expect=0)
        self.run_verb("stop", expect=0)
        self.run_verb("status", expect=3)
        self.fill(ALARM)
        done = self.run_verb("status", expect=1)
        self.assertIn("disk", done.stdout)

    def test_no_gap_state_is_left_behind(self):
        self.fill(7)
        self.configure(self.archive)
        self.run_verb("start", expect=0)
        self.run_verb("archive", expect=0)
        state = sorted(p.name for p in (self.folder / "state").iterdir())
        self.assertEqual(state, ["started"])


class RotationAndDocs(unittest.TestCase):
    def test_the_collector_deletes_no_rotated_file(self):
        # max_backups 0 and no max_days: timberjack's millRunOnce returns
        # before deleting anything. archive is the only deleter.
        text = (FOLDER / "collector.yaml").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"max_backups: (\d+)", text)), {"0"})
        self.assertNotIn("max_days:", text)

    def test_help_lists_archive(self):
        text = (FOLDER / "genai-traces.sh").read_text(encoding="utf-8")
        help_text = text.split("HELPEOF", 2)[1]
        self.assertIn("  archive ", help_text)
        self.assertIn("GENAI_TRACES_ARCHIVE_DIR", help_text)

    def test_the_example_names_a_placeholder_folder(self):
        lines = (FOLDER / ".env.example").read_text(encoding="utf-8").splitlines()
        line = next(line for line in lines if "GENAI_TRACES_ARCHIVE_DIR=" in line)
        self.assertTrue(line.startswith("#"), line)
        self.assertIn("change-me", line)


if __name__ == "__main__":
    unittest.main()
