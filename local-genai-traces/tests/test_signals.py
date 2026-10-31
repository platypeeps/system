"""Metrics and logs land beside traces (system sd:6942).

Claude Code's native OTel export sends metrics and log events, not spans.
collector.yaml gives each signal a pipeline of its own: otlp in, batch,
one raw file out (storage/raw/<signal>.jsonl) with the rotation of
traces.jsonl. Phoenix takes traces only. `archive` moves the rotated
metrics and logs files as it moves traces, and they count toward the
backlog alarm. Same stubs as test_status.py.
"""

import gzip
import re
import unittest

from .test_archive import ALARM, Archive
from .test_status import FOLDER, StatusCase


def collector_config():
    """{exporter or pipeline name: its indented block}, from collector.yaml.

    No YAML parser in the stdlib: this reads the two-space layout the file
    keeps, one level under `exporters:` and under `service: pipelines:`.
    """
    text = (FOLDER / "collector.yaml").read_text(encoding="utf-8")
    exporters = text.split("\nexporters:\n", 1)[1].split("\n\n", 1)[0]
    pipelines = text.split("  pipelines:\n", 1)[1]
    blocks = {}
    for section, indent in ((exporters, "  "), (pipelines, "    ")):
        name = None
        for line in section.splitlines():
            head = re.match(rf"^{indent}([^\s#][^:]*):\s*$", line)
            if head:
                name = head.group(1)
                blocks[name] = ""
            elif name and line.startswith(indent + " "):
                blocks[name] += line.strip() + "\n"
    return blocks


class Config(unittest.TestCase):
    def test_metrics_and_logs_each_have_a_pipeline_to_their_own_file(self):
        blocks = collector_config()
        for signal in ("metrics", "logs"):
            pipeline = blocks[signal]
            self.assertIn("receivers: [otlp]", pipeline)
            self.assertIn("processors: [batch]", pipeline)
            self.assertIn(f"exporters: [file/{signal}]", pipeline)
            exporter = blocks[f"file/{signal}"]
            self.assertIn(f"path: /out/{signal}.jsonl", exporter)
            self.assertIn("format: json", exporter)
            self.assertIn("max_megabytes: 100", exporter)
            self.assertIn("max_backups: 0", exporter)

    def test_phoenix_takes_traces_only(self):
        blocks = collector_config()
        self.assertIn("otlp/phoenix", blocks["traces"])
        for signal in ("metrics", "logs"):
            self.assertNotIn("phoenix", blocks[signal])


class ArchiveSignals(StatusCase):
    """A live and a rotated file per signal; Archive's tests count traces
    files alone, so this class shares only its configure."""

    SIGNALS = ("traces", "metrics", "logs")
    STAMP = "2026-10-22T07-08-09.012"
    configure = Archive.configure

    def setUp(self):
        super().setUp()
        self.raw = self.folder / "storage" / "raw"
        self.raw.mkdir(parents=True)
        self.archive = self.root / "share" / "genai-traces"
        self.archive.mkdir(parents=True)
        self.contents = {}
        for signal in self.SIGNALS:
            for name in (f"{signal}.jsonl", f"{signal}-{self.STAMP}.jsonl"):
                body = f'{{"{signal}":"{name}"}}\n'.encode() * 50
                (self.raw / name).write_bytes(body)
                self.contents[name] = body

    def test_rotated_metrics_and_logs_move_and_the_live_files_stay(self):
        self.configure(self.archive)
        self.run_verb("archive", expect=0)
        for signal in self.SIGNALS:
            rotated = f"{signal}-{self.STAMP}.jsonl"
            self.assertEqual(
                gzip.decompress((self.archive / (rotated + ".gz")).read_bytes()),
                self.contents[rotated])
            self.assertFalse((self.raw / rotated).exists())
            live = f"{signal}.jsonl"
            self.assertEqual((self.raw / live).read_bytes(), self.contents[live])
            self.assertFalse((self.archive / (live + ".gz")).exists())

    def test_metrics_and_logs_count_toward_the_backlog(self):
        for n in range(ALARM // 2):
            (self.raw / f"metrics-2026-09-01T00-00-{n:02d}.000.jsonl").write_bytes(b"x\n")
            (self.raw / f"logs-2026-09-01T00-00-{n:02d}.000.jsonl").write_bytes(b"x\n")
        for signal in self.SIGNALS:
            (self.raw / f"{signal}-{self.STAMP}.jsonl").unlink()
        done = self.run_verb("status", expect=1)
        self.assertIn("disk", done.stdout)


if __name__ == "__main__":
    unittest.main()
