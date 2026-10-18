"""sd:5112: sources pasted or dropped on the Research page's Sources tab land as ledger rows through the lane.

A research checkout with a bare origin stands in for `research/reading`; `sd` and `sd-ship` are stubs on PATH, and the
title fetch is the source review's `_fetch`, patched, so nothing leaves the machine. Each case checks the operator's
checkout is left as it was: its HEAD, its working tree and its ledger.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from sd_db import workflow, writes
from sd_dashboard import research_screen

from test_workflow_actions import BrowserSession

LEDGER = """# Source Registry

| Grade | Source | Read | Status |
| --- | --- | --- | --- |
| A | [The old spec](https://example.test/old) | 2026-10-01 | read in full |

## Corrections on record

None yet.
"""
IDS = """# Sources

| ID | Source | Reviewed | Grade |
| --- | --- | --- | --- |
| S1 | [One](https://example.test/one) | 2026-10-01 | A |
| S3 | [Three](https://example.test/three) | 2026-10-02 | B |
"""
SD = """#!/bin/sh
[ "$1 $2 $3" = "config get sd.bulk_storage_root" ] && [ -n "$FIXTURE_BULK" ] && { echo "$FIXTURE_BULK"; exit 0; }
exit 1
"""
SD_SHIP = """#!/bin/sh
printf '%s\\n' "$@" > "$FIXTURE_SHIP_ARGV"
[ -n "$FIXTURE_SHIP_FAIL" ] && { echo '{"ok": false, "error": "the hub did not answer"}'; exit 4; }
echo '{"ok": true}'
"""
TITLED = "Fetched https://example.test/spec: HTTP 200, text/html, 120 bytes.\nIts title: The Spec | v2.\n\nThe text.\n"


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=True,
                          timeout=30).stdout.strip()


class SourceIntake(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.root, self.home, bin_ = root, root / "home", root / "bin"
        for folder in (self.home, bin_):
            folder.mkdir()
        for name, text in (("sd", SD), ("sd-ship", SD_SHIP)):
            (bin_ / name).write_text(text)
            (bin_ / name).chmod(0o755)
        self.ship = root / "ship.argv"
        environment = patch.dict(os.environ, {
            "PATH": f"{bin_}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(self.home),
            "REPO_ROOT": str(root / "repos"), "XDG_STATE_HOME": str(root / "state"), "FIXTURE_SHIP_ARGV": str(self.ship),
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test", "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.test", "GIT_CONFIG_GLOBAL": os.devnull})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("FIXTURE_BULK", "FIXTURE_SHIP_FAIL"):
            os.environ.pop(name, None)
        fetch = patch("sd_dashboard.adversarial._fetch", return_value=TITLED)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.reading = root / "repos" / "research" / "reading"
        git(root, "clone", "-q", str(self.origin), str(self.reading))
        (self.reading / "research.conf.py").write_text("DOCS = []\n")
        self.publish(LEDGER)
        self.repo(str(self.reading))

    def publish(self, ledger, *extra):
        """Commit `ledger` (and `extra` (path, bytes) files) in the checkout and push it, as origin/main."""
        (self.reading / "10-sources").mkdir(exist_ok=True)
        (self.reading / "10-sources" / "registry.md").write_text(ledger)
        for path, data in extra:
            (self.reading / path).parent.mkdir(parents=True, exist_ok=True)
            (self.reading / path).write_bytes(data)
        git(self.reading, "add", "-A")
        git(self.reading, "commit", "-q", "-m", "ledger")
        git(self.reading, "push", "-q", "origin", "main")
        self.before = (git(self.reading, "rev-parse", "HEAD"), (self.reading / "10-sources" / "registry.md").read_text())

    def assertCheckoutUntouched(self):
        self.assertEqual(git(self.reading, "status", "--porcelain"), "")
        self.assertEqual((git(self.reading, "rev-parse", "HEAD"), (self.reading / "10-sources" / "registry.md").read_text()),
                         self.before)
        self.assertEqual(git(self.reading, "branch", "--show-current"), "main")

    def assertNothingLeft(self):
        self.assertEqual(git(self.reading, "branch", "--list", "source-intake/*"), "")
        self.assertFalse(list((self.home / "worktrees").glob("*")) if (self.home / "worktrees").exists() else [])
        self.assertFalse(self.ship.exists())

    def intake(self, **payload):
        return self.post("/api/research/intake", {"target": "research/reading", **payload})

    def drop(self, name, data):
        request = urllib.request.Request(self.base + "/api/research/intake/file", data=data, method="POST", headers={
            "Content-Type": "application/octet-stream", "Origin": self.base, "Cookie": self.cookie,
            "X-SD-CSRF": self.csrf, "Sec-Fetch-Site": "same-origin", "X-SD-File-Name": urllib.parse.quote(name)})
        try:
            answer = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            answer = error
        with answer:
            return answer.status, json.loads(answer.read())

    def added(self, answer):
        work = Path(answer["worktree"])
        return work, (work / "10-sources" / "registry.md").read_text().splitlines()

    def day(self, answer):
        stamp = answer["branch"].split("/")[1]
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"

    def test_a_pasted_url_is_a_grade_u_row_on_a_fresh_branch_and_enqueued(self):
        status, _, answer = self.intake(urls=["https://example.test/spec"])
        self.assertEqual(status, 200, answer)
        work, lines = self.added(answer)
        day = self.day(answer)
        self.assertIn(f"| U | [The Spec - v2](https://example.test/spec) | — | added {day}, not yet read |", lines)
        # The row is the table's: after its last row, before the next section.
        self.assertEqual(lines.index("## Corrections on record") - 2, lines.index(next(l for l in lines if "The Spec" in l)))
        self.assertTrue(work.is_relative_to(self.home / "worktrees"), work)
        self.assertEqual(git(work, "branch", "--show-current"), answer["branch"])
        self.assertEqual(git(work, "rev-parse", "HEAD^"), self.before[0])
        self.assertTrue(git(work, "log", "-1", "--format=%B").rstrip().endswith("Authored-with: script"))
        item = writes.item_by_external(self.connection, "task-ref", f"source-intake:{answer['branch'].split('/')[1]}")
        self.assertEqual(item["id"], answer["item"])
        self.assertEqual(item["repo"], str(self.reading))
        argv = self.ship.read_text().splitlines()
        self.assertEqual(argv[:4], ["-C", str(work), "lane", "enqueue"])
        self.assertEqual(argv[argv.index("--item") + 1], str(answer["item"]))
        self.assertIn("--deliver", argv)
        self.assertIn(f"sd:{answer['item']}", Path(argv[argv.index("--body-file") + 1]).read_text())
        self.assertTrue(answer["enqueued"])
        self.assertEqual(answer["added"], [{"kind": "url", "title": "The Spec - v2", "key": "https://example.test/spec"}])
        self.assertCheckoutUntouched()

    def test_a_page_without_a_title_is_named_by_its_host_and_path(self):
        self.fetch.return_value = "Not fetched: URLError: refused. Whether the page is still there is not known.\n"
        status, _, answer = self.intake(urls=["https://example.test/docs/limits/"])
        self.assertEqual(status, 200, answer)
        self.assertIn("| U | [example.test/docs/limits](https://example.test/docs/limits/) |", "\n".join(self.added(answer)[1]))

    def test_an_id_column_takes_the_next_free_s_id(self):
        self.publish(IDS)
        status, _, answer = self.intake(urls=["https://example.test/a", "https://example.test/b"])
        self.assertEqual(status, 200, answer)
        lines, day = self.added(answer)[1], self.day(answer)
        # No Status or Note column: the notes go in the last free one.
        self.assertEqual(lines[-2:], [f"| S4 | [The Spec - v2](https://example.test/a) | added {day}, not yet read | U |",
                                      f"| S5 | [The Spec - v2](https://example.test/b) | added {day}, not yet read | U |"])
        self.assertEqual([one["key"] for one in answer["added"]], ["https://example.test/a", "https://example.test/b"])

    def test_a_small_dropped_file_is_committed_under_10_sources_files(self):
        status, staged = self.drop("Vendor note (draft).PDF", b"%PDF-1.4 small")
        self.assertEqual(status, 200, staged)
        self.assertEqual(staged["name"], "Vendor-note-draft.pdf")
        status, _, answer = self.intake(files=[staged])
        self.assertEqual(status, 200, answer)
        work, lines = self.added(answer)
        self.assertEqual((work / "10-sources" / "files" / "Vendor-note-draft.pdf").read_bytes(), b"%PDF-1.4 small")
        self.assertIn("| U | [Vendor-note-draft.pdf](files/Vendor-note-draft.pdf) | — |", "\n".join(lines))
        self.assertEqual(git(work, "show", "--name-only", "--format=", "HEAD").splitlines(),
                         ["10-sources/files/Vendor-note-draft.pdf", "10-sources/registry.md"])
        # The Research page keys the row by the file it links, so the source review can name it.
        _, rows = research_screen.parse_registry("\n".join(lines), "10-sources/registry.md")
        self.assertEqual(research_screen.row_key(rows[-1]), "file:10-sources/files/Vendor-note-draft.pdf")
        self.assertEqual(answer["added"][0]["key"], "file:10-sources/files/Vendor-note-draft.pdf")
        self.assertFalse(list((self.root / "state" / "system" / "source-intake" / "staged").iterdir()))
        self.assertCheckoutUntouched()

    def test_a_large_dropped_file_goes_under_the_bulk_root_and_the_row_links_it(self):
        bulk = self.root / "bulk"
        os.environ["FIXTURE_BULK"] = str(bulk)
        with patch("sd_dashboard.source_intake.FILE_BYTES", 8):
            status, staged = self.drop("big.txt", b"more than eight bytes")
            status, _, answer = self.intake(files=[staged])
        self.assertEqual(status, 200, answer)
        work, lines = self.added(answer)
        stored = bulk / "reading" / "sources" / "big.txt"
        self.assertEqual(stored.read_bytes(), b"more than eight bytes")
        self.assertIn(f"| U | [big.txt]({stored}) | — |", "\n".join(lines))
        self.assertEqual(git(work, "show", "--name-only", "--format=", "HEAD").splitlines(), ["10-sources/registry.md"])
        self.assertTrue(answer["added"][0]["bulk"])

    def test_a_large_file_with_no_bulk_root_is_refused_and_nothing_is_left(self):
        with patch("sd_dashboard.source_intake.FILE_BYTES", 8):
            _, staged = self.drop("big.txt", b"more than eight bytes")
            status, _, answer = self.intake(files=[staged])
        self.assertEqual(status, 400, answer)
        self.assertIn("big.txt is over", answer["error"])
        self.assertNothingLeft()
        self.assertCheckoutUntouched()

    def test_a_failure_after_the_bulk_copy_and_commit_undoes_both(self):
        bulk = self.root / "bulk"
        os.environ["FIXTURE_BULK"] = str(bulk)
        with patch("sd_dashboard.source_intake.FILE_BYTES", 8), \
                patch("sd_dashboard.server.capture_ref", side_effect=workflow.WorkflowError("the item was not filed")):
            _, staged = self.drop("big.txt", b"more than eight bytes")
            status, _, answer = self.intake(files=[staged], urls=["https://example.test/spec"])
        self.assertEqual(status, 400, answer)
        self.assertIn("the item was not filed", answer["error"])
        self.assertEqual(list((bulk / "reading" / "sources").iterdir()), [])
        self.assertNothingLeft()
        self.assertCheckoutUntouched()
        # The staged file stays, so the same drop can be added again.
        self.assertTrue((self.root / "state" / "system" / "source-intake" / "staged" / staged["sha256"]).is_file())

    def test_a_duplicate_url_or_file_is_refused_by_name_and_nothing_is_written(self):
        self.publish(LEDGER, ("10-sources/files/kept.md", b"# kept\n"))
        _, same = self.drop("again.md", b"# kept\n")
        for payload, said in (({"urls": ["https://example.test/old/"]}, "https://example.test/old/"),
                              ({"files": [same]}, "again.md (the same file as kept.md)"),
                              ({"urls": ["https://example.test/new", "https://example.test/new#top"]},
                               "https://example.test/new#top")):
            with self.subTest(payload=payload):
                status, _, answer = self.intake(**payload)
                self.assertEqual(status, 400, answer)
                self.assertIn("Not added, and nothing was written", answer["error"])
                self.assertIn(said, answer["error"])
                self.assertNothingLeft()
                self.assertCheckoutUntouched()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM item WHERE external_id LIKE 'source-intake:%'")
                         .fetchone()[0], 0)

    def test_sources_in_one_batch_that_repeat_each_other_are_refused_before_any_write(self):
        # Round 1 class: each source was checked against the checkout, not against the others in its batch.
        bulk = self.root / "bulk"
        os.environ["FIXTURE_BULK"] = str(bulk)
        with patch("sd_dashboard.source_intake.FILE_BYTES", 8):
            first, second = self.drop("report?.txt", b"one"), self.drop("report!.txt", b"two")
            self.assertEqual((first[1]["name"], second[1]["name"]), ("report.txt", "report.txt"))
            cases = (
                ([first[1], second[1]], "report.txt (another file dropped with it is stored under that name)"),
                # A case-insensitive disk holds these as one file.
                ([self.drop("Notes.md", b"upper")[1], self.drop("notes.md", b"lower")[1]],
                 "notes.md (another file dropped with it is stored under that name)"),
                ([self.drop("same.md", b"same")[1], self.drop("same.md", b"same")[1]],
                 "same.md (the same file as same.md, dropped with it)"),
                # One copy would go to the checkout and one under the bulk root; the name is one in the ledger.
                ([self.drop("scan.pdf", b"small")[1], self.drop("scan!.pdf", b"over eight bytes")[1]],
                 "scan.pdf (another file dropped with it is stored under that name)"))
            for files, said in cases:
                with self.subTest(said=said):
                    status, _, answer = self.intake(files=files)
                    self.assertEqual(status, 400, answer)
                    self.assertIn(said, answer["error"])
                    self.assertNothingLeft()
                    self.assertCheckoutUntouched()
                    self.assertFalse((bulk / "reading" / "sources").exists() and
                                     list((bulk / "reading" / "sources").iterdir()))
                    for one in files:
                        self.assertTrue((self.root / "state" / "system" / "source-intake" / "staged" /
                                         one["sha256"]).is_file())

    def test_a_url_or_s_id_another_ledger_of_the_checkout_holds_is_taken(self):
        # The rows go in registry.md, but SOURCES.md is a ledger of the same checkout and the claim map reads both.
        self.publish(IDS, ("SOURCES.md", b"# Older\n\n| ID | Source | Note |\n| --- | --- | --- |\n"
                                         b"| S7 | [Elsewhere](https://example.test/elsewhere) | kept |\n"))
        status, _, answer = self.intake(urls=["https://example.test/elsewhere"])
        self.assertEqual(status, 400, answer)
        self.assertIn("https://example.test/elsewhere (already in SOURCES.md)", answer["error"])
        self.assertNothingLeft()
        status, _, answer = self.intake(urls=["https://example.test/a"])
        self.assertEqual(status, 200, answer)
        self.assertTrue(self.added(answer)[1][-1].startswith("| S8 | [The Spec - v2](https://example.test/a) |"),
                        self.added(answer)[1][-1])

    def test_a_failed_enqueue_keeps_the_commit_and_notes_the_line_that_retries(self):
        os.environ["FIXTURE_SHIP_FAIL"] = "1"
        status, _, answer = self.intake(urls=["https://example.test/spec"])
        self.assertEqual(status, 200, answer)
        self.assertFalse(answer["enqueued"])
        self.assertIn("the hub did not answer", answer["error"])
        self.assertTrue(Path(answer["worktree"]).is_dir())
        notes = [note["body"] for note in workflow.item_state(self.connection, answer["item"])["notes"]
                 if note["kind"] == "comment"]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("lane enqueue --item", notes[0])
        self.assertCheckoutUntouched()

    def test_an_unregistered_checkout_another_suffix_or_shape_is_refused(self):
        status, answer = self.drop("tool.exe", b"MZ")
        self.assertEqual(status, 400, answer)
        self.assertIn("tool.exe is not one of", answer["error"])
        self.connection.execute("DELETE FROM repo WHERE path = ?", (str(self.reading),))
        self.connection.commit()
        for payload, said in (({"urls": ["https://example.test/x"]}, "research/reading is not a registered repository"),
                              ({"urls": ["ftp://example.test/x"]}, "is not an http or https address"),
                              ({"urls": []}, "Paste a URL or drop a file"),
                              ({"urls": ["https://example.test/x"], "grade": "A"}, "and nothing else")):
            with self.subTest(payload=payload):
                status, _, answer = self.intake(**payload)
                self.assertEqual(status, 400, answer)
                self.assertIn(said, answer["error"])
        self.assertNothingLeft()

    def test_no_target_means_the_reading_checkout(self):
        status, _, answer = self.post("/api/research/intake", {"urls": ["https://example.test/spec"]})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["repo"], str(self.reading))
