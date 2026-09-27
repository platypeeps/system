"""The journal move of `removal.apply`, read back through the runner's own readers (sd:754, PR 2).

Each test builds a temporary home with a `Config`, a store outside
`default_path(home)`, a retention root, and the probe fixture's two released
runs with their journal pair written by `runner_journal.persist`. After an
apply, `reconciliation.plan` must show no entry for a removed run and
`archive_refresh._safe_state` must not raise, because a journal record whose
row is gone is a blocked "assignment is absent" entry that holds archive
refresh for every run (note 1900). The controls put the files back and see
the hold, so the tests can tell a move from a skipped one.

The interrupt, stop-point and signal tests of `implement.md` step 5 live
here too, beside the readers they protect. Against `origin/main` code with no
`sd_db/removal.py` this module fails to import.
"""

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from sd_db import removal, reporting
from sd_db import runner_journal as journal
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.operations import control_gate
from sd_db.writes import add_note, create_assignment, create_item, upsert_repo
from sd_runner import archive_refresh, reconciliation
from sd_runner.runtime import Config

backup = removal.backups
STAMP = "2026-09-09T12:00:00+00:00"
WHO = {"who": "operator", "reason": "the probe is finished", "session": None, "principal": "operator",
       "program": "sd-db.sh repo remove"}
HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from sd_db import runner_journal
    with runner_journal.lock(Path(sys.argv[1])):
        print("held", flush=True)
        time.sleep(60)
    """)


class Home(unittest.TestCase):
    prefix = "sd754-"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=self.prefix, suffix=".noindex")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"; self.home.mkdir()
        self.database = self.root / "db" / "sd.db"
        initialise(self.database)
        self.db = connect(self.database); self.addCleanup(self.db.close)
        self.retained = self.root / "retained"; self.retained.mkdir()
        self.config = Config(self.database, self.root / "work", self.retained, self.root / "pack", self.home,
                             floor_gb=.001)
        self.source = str(self.root / "checkouts" / "provisioning-probe")
        upsert_repo(self.db, self.source)
        self.probe = create_item(self.db, kind="task", title="runner provisioning probe", status="done",
                                 repo=self.source)
        add_note(self.db, self.probe, "decision", "went fine", session="operator")
        self.works = [create_assignment(self.db, role="author", status="done", item=self.probe) for _ in range(2)]
        self.runs = [self.attempt(work) for work in self.works]
        # A newer assignment on another item, so A1 clears.
        create_assignment(self.db, role="author", status="done",
                          item=create_item(self.db, kind="task", title="later work", status="done"))
        self.journal = journal.directory(self.database)
        for run in self.runs:
            journal.persist(self.database, dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (run,)).fetchone()))
        self.pairs = [[self.journal / f"{run}.json", self.journal / f"{run}.lock"] for run in self.runs]
        self.files = [path for pair in self.pairs for path in pair]
        self.plan = removal.plan_repo(self.db, self.source, with_items=True, home=self.home)
        self.assertEqual(self.plan["refusals"], [])
        self.assertEqual(self.plan["move"]["files"], [str(path) for path in self.files])
        self.fingerprint = self.plan["fingerprint"]
        self.quarantine = self.database.parent / "runner-recovery-evidence" / f"removed-{self.fingerprint}"
        self.before = self.counts()

    def attempt(self, assignment, number=1):
        ident = uuid.uuid4().hex
        retained = self.retained / str(assignment) / str(number) / "clone"
        self.db.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, created_at,"
            " updated_at, released_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ident, assignment, number, self.source, "sd/probe", "runner",
             str(self.root / "work" / str(self.probe) / f"{assignment}-{number}-{ident}"), str(retained), STAMP, STAMP, STAMP))
        self.db.execute("INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at, released_at)"
                        " VALUES (?,?,?,?,?,?)", (ident, self.source, "sd/probe", 1, STAMP, STAMP))
        return ident

    # -- helpers ------------------------------------------------------------

    def counts(self, connection=None):
        connection = connection or self.db
        return {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def unchanged_but_state(self, gained):
        after = self.counts()
        self.assertEqual({k: v for k, v in after.items() if k != "state"},
                         {k: v for k, v in self.before.items() if k != "state"})
        self.assertEqual(after["state"] - self.before["state"], gained)

    def apply(self, **changes):
        return removal.apply(self.db, "repo", self.source, fingerprint=self.fingerprint, home=self.home,
                             with_items=True, **{**WHO, **changes})

    def gone(self):
        self.assertEqual(self.db.execute("SELECT count(*) FROM runner_run").fetchone()[0], 0)
        self.assertIsNone(self.db.execute("SELECT 1 FROM item WHERE id=?", (self.probe,)).fetchone())

    def record_id(self):
        return self.db.execute("SELECT id FROM item WHERE source='cron-report' AND external_id=?",
                               (f"repo-remove:{self.fingerprint}",)).fetchone()["id"]

    def entries(self):
        report = reconciliation.plan(self.config)
        return report["entries"], report["journal_issues"]

    def safe(self):
        archive_refresh._safe_state(self.config)

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def hold(self, path):
        """A child process that holds `runner_journal.lock` on `path` until it is killed."""
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.kill, child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    @staticmethod
    def kill(child):
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait()

    @staticmethod
    def run_line(line, check=True):
        return subprocess.run(["sh", "-c", line], check=check, capture_output=True, text=True)

    @contextmanager
    def after_backup(self, change):
        """`change()` runs once the backup is written: the backup validates the journal, so a bad entry
        reaches step 7 only when it appears after the backup (`backup._runner_files` refuses it before)."""
        real = backup.run

        def run(**kwargs):
            snapshot = real(**kwargs)
            change()
            return snapshot

        with mock.patch.object(backup, "run", side_effect=run):
            yield

    def put_back(self, record, *, move_files=True):
        """`design.md` section 4.3, steps 2 to 5: the rows from the chunk notes, then the journal pair."""
        rows = []
        for (body,) in self.db.execute("SELECT body FROM note WHERE item=? AND kind='comment' ORDER BY id", (record,)):
            header, _, rest = body.partition("\n")
            lines = rest.split("\n")
            self.assertEqual(header.split("sha256 ")[1], hashlib.sha256("\n".join(lines).encode()).hexdigest())
            rows += [json.loads(line) for line in lines]
        with control_gate(self.db):
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for table in removal.INSERT_ORDER:
                    for entry in [row for row in rows if row["table"] == table]:
                        columns = list(entry["row"])
                        self.db.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                                        [entry["row"][column] for column in columns])
                self.assertEqual(list(self.db.execute("PRAGMA foreign_key_check")), [])
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            if move_files:
                receipt = json.loads((self.quarantine / "receipt.json").read_text())
                named = {Path(entry["source"]).name: entry["sha256"] for entry in receipt["files"]}
                for path in sorted(self.quarantine.iterdir()):
                    if path.name == "receipt.json":
                        continue
                    self.assertEqual(self.digest(path), named[path.name])
                    self.assertFalse((self.journal / path.name).exists())
                for suffix in (".json", ".lock"):
                    for name in [name for name in named if name.endswith(suffix)]:
                        os.rename(self.quarantine / name, self.journal / name)
        add_note(self.db, record, "comment", "put back by the test", session="operator")


class TheMove(Home):

    def test_the_pair_leaves_the_journal_and_the_runner_sees_no_hold(self):
        self.assertEqual(self.entries(), ([], []))
        originals = {path.name: self.digest(path) for path in self.files}
        result = self.apply()
        self.gone()
        entries, issues = self.entries()
        self.assertEqual([entry for entry in entries if entry["run"] in self.runs], [])
        self.assertEqual(issues, [])
        self.safe()
        self.assertEqual(sorted(path.name for path in self.journal.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.quarantine.iterdir()),
                         sorted(["receipt.json", *originals]))
        for name, digest in originals.items():
            self.assertEqual(self.digest(self.quarantine / name), digest)
        receipt = json.loads((self.quarantine / "receipt.json").read_text())
        self.assertEqual(receipt["record"], self.record_id())
        self.assertEqual(receipt["fingerprint"], self.fingerprint)
        self.assertEqual([entry["source"] for entry in receipt["files"]], [str(path) for path in self.files])
        self.assertEqual(result["move_commands"], [])
        self.assertIsNone(result["move_error"])
        self.assertEqual(result["moved"], [str(path) for path in self.files])
        self.assertEqual(oct(self.quarantine.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.quarantine / "receipt.json").stat().st_mode & 0o777), "0o600")

    def test_control_a_record_put_back_without_its_row_is_the_hold(self):
        self.apply()
        for pair in self.pairs:
            shutil.copy2(self.quarantine / pair[0].name, pair[0])
        entries, issues = self.entries()
        self.assertEqual(sorted(entry["run"] for entry in entries), sorted(self.runs))
        self.assertEqual({entry["blocked"] for entry in entries},
                         {"assignment is absent; restore a database backup containing its original work"})
        with self.assertRaisesRegex(Exception, "archive refresh held"):
            self.safe()

    def test_a_held_ending_lock_fails_the_move_after_the_commit(self):
        ending = self.database.parent / "runner-ending" / f"{self.runs[0]}.lock"
        self.hold(ending)
        result = self.apply()
        self.gone()
        self.record_id()
        self.assertIn(str(ending), result["move_error"])
        self.assertIn("ending sequence", result["move_error"])
        self.assertIn("archive refresh", result["move_error"])
        self.assertEqual(sorted(path.name for path in self.journal.iterdir()), sorted(path.name for path in self.files))
        self.assertEqual(len(result["move_commands"]), 6)
        self.assertEqual(result["moved"], [])

    def test_a_held_journal_lock_stops_the_move_and_the_lines_finish_it(self):
        child = self.hold(self.pairs[0][1])
        result = self.apply()
        self.gone()
        record = self.record_id()
        self.assertIn(str(self.pairs[0][1]), result["move_error"])
        lines = result["move_commands"]
        self.assertEqual(lines[:2], [f"mkdir -m 700 {shlex.quote(str(self.quarantine.parent))}",
                                     f"mkdir -m 700 {shlex.quote(str(self.quarantine))}"])
        self.assertEqual(len(lines), 6)
        for line, source in zip(lines[2:], self.files):
            with self.subTest(source=source.name):
                self.assertEqual(shlex.split(line), ["mv", "-n", str(source), str(self.quarantine / source.name),
                                                     "&&", "test", "!", "-e", str(source),
                                                     "&&", "test", "!", "-L", str(source)])
        self.assertTrue(all(pair[0].exists() for pair in self.pairs))
        entries, _ = self.entries()
        self.assertEqual(len(entries), 2)
        self.kill(child)
        for line in lines:
            self.run_line(line)
        self.assertEqual(self.entries(), ([], []))
        self.safe()
        self.assertEqual(sorted(path.name for path in self.quarantine.iterdir()), sorted(path.name for path in self.files))
        self.assertIn(f"file {self.pairs[0][0]}", json.loads(self.db.execute(
            "SELECT body FROM item WHERE id=?", (record,)).fetchone()[0])["text"].split("\n"))

    def test_an_existing_target_fails_its_line_and_keeps_the_placed_file(self):
        child = self.hold(self.pairs[0][1])
        lines = self.apply()["move_commands"]
        self.kill(child)
        for line in lines[:2]:
            self.run_line(line)
        placed = self.quarantine / self.pairs[0][0].name
        placed.write_text("placed by the test")
        digest = self.digest(placed)
        self.assertNotEqual(self.run_line(lines[2], check=False).returncode, 0)
        self.assertEqual(self.digest(placed), digest)
        self.assertTrue(self.pairs[0][0].exists())

    def dangle(self):
        self.pairs[0][1].unlink()
        self.pairs[0][1].symlink_to(self.root / "nowhere.lock")

    def test_a_dangling_lock_symlink_is_refused_and_its_line_fails(self):
        with self.after_backup(self.dangle):
            result = self.apply()
        self.gone()
        self.assertIn(str(self.pairs[0][1]), result["move_error"])
        self.assertIn("not a regular file", result["move_error"])
        self.assertFalse((self.root / "nowhere.lock").exists())
        lines = result["move_commands"]
        self.assertEqual([shlex.split(line)[2] for line in lines[2:]], [str(path) for path in self.files])
        for line in lines[:2]:
            self.run_line(line)
        (self.quarantine / self.pairs[0][1].name).write_text("")
        self.assertNotEqual(self.run_line(lines[3], check=False).returncode, 0)
        self.assertTrue(self.pairs[0][1].is_symlink())

    def test_the_step_7_checks(self):
        # A `.lock` symlink is refused before `lock`, so its target is not created.
        with self.after_backup(self.dangle), mock.patch.object(journal, "lock", wraps=journal.lock) as lock:
            result = self.apply()
        self.assertIn("not a regular file", result["move_error"])
        self.assertFalse((self.root / "nowhere.lock").exists())
        self.assertEqual([call.args[0] for call in lock.call_args_list if call.args[0] == self.pairs[0][1]], [])
        self.assertFalse(self.quarantine.exists())

    def test_an_existing_quarantine_directory_is_refused_and_nothing_is_renamed(self):
        self.quarantine.parent.mkdir(mode=0o700)
        self.quarantine.mkdir(mode=0o700)
        result = self.apply()
        self.gone()
        self.assertIn("exists", result["move_error"])
        self.assertEqual(result["moved"], [])
        self.assertEqual(sorted(path.name for path in self.journal.iterdir()), sorted(path.name for path in self.files))
        self.assertEqual(sorted(self.quarantine.iterdir()), [])
        self.assertEqual(result["move_commands"][0].split()[0], "mv")

    def test_the_receipt_exists_before_the_first_rename(self):
        real = os.rename

        def rename(source, target):
            self.assertTrue((self.quarantine / "receipt.json").exists(), "receipt.json before the first rename")
            self.assertEqual(json.loads((self.quarantine / "receipt.json").read_text())["record"], self.record_id())
            return real(source, target)

        with mock.patch.object(removal.os, "rename", side_effect=rename) as patched:
            result = self.apply()
        self.assertEqual(patched.call_count, 4)
        self.assertIsNone(result["move_error"])

    def test_another_entry_after_the_backup_is_refused_before_the_commit(self):
        """The #350 review: the re-plan inside the transaction names a stray `.partial`,
        so the rows stay rather than go with the journal left behind for a run with no row."""
        partial = self.journal / f"{self.runs[0]}.partial"
        with self.after_backup(lambda: partial.write_text("{")), \
                self.assertRaisesRegex(removal.RemovalRefused, partial.name):
            self.apply()
        self.unchanged_but_state(1)
        self.assertTrue(all(path.exists() for path in self.files))
        self.assertTrue(partial.exists())
        self.assertFalse(self.quarantine.exists())

    def test_another_entry_beside_the_pair_is_refused_before_anything_moves(self):
        """Note 1966, Q3: a `.partial` is the runner's to reconcile; the pair stays beside it.

        One that appears after the re-plan, inside the transaction, is past the
        plan's check; the move's own listing still refuses it."""
        partial = self.journal / f"{self.runs[0]}.partial"
        real = removal._delete

        def delete(connection, plan):
            partial.write_text("{")
            return real(connection, plan)

        with mock.patch.object(removal, "_delete", side_effect=delete):
            result = self.apply()
        self.gone()
        self.assertIn(partial.name, result["move_error"])
        self.assertEqual(result["moved"], [])
        self.assertTrue(all(path.exists() for path in self.files))
        self.assertTrue(partial.exists())
        self.assertEqual(len(result["move_commands"]), 6)

    def test_a_linked_evidence_directory_is_refused_and_gives_no_line(self):
        """Copilot on #407: a `mv` through a link would carry the files outside the store."""
        elsewhere = self.root / "elsewhere"; elsewhere.mkdir(mode=0o700)
        # The backup refuses a linked evidence directory itself; the link
        # appears after it, as the other step 7 cases do.
        with self.after_backup(lambda: self.quarantine.parent.symlink_to(elsewhere)):
            result = self.apply()
        self.gone()
        self.assertIn("recovery directory must be private, owned and unlinked", result["move_error"])
        self.assertEqual(result["move_commands"], [])
        self.assertEqual(result["moved"], [])
        self.assertTrue(all(path.exists() for path in self.files))
        self.assertEqual(sorted(elsewhere.iterdir()), [])

    def test_a_json_only_journal_moves_the_json_alone(self):
        """Copilot on #407: the lock file `lock` creates is not evidence; it goes, and the receipt is whole."""
        self.pairs[0][1].unlink()
        plan = removal.plan_repo(self.db, self.source, with_items=True, home=self.home)
        self.fingerprint = plan["fingerprint"]
        self.quarantine = self.database.parent / "runner-recovery-evidence" / f"removed-{self.fingerprint}"
        planned = [self.pairs[0][0], *self.pairs[1]]
        self.assertEqual(plan["move"]["files"], [str(path) for path in planned])
        result = self.apply()
        self.assertIsNone(result["move_error"])
        self.assertEqual(result["moved"], [str(path) for path in planned])
        self.assertEqual(sorted(path.name for path in self.journal.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.quarantine.iterdir()),
                         sorted(["receipt.json", *(path.name for path in planned)]))
        receipt = json.loads((self.quarantine / "receipt.json").read_text())
        self.assertEqual([entry["source"] for entry in receipt["files"]], [str(path) for path in planned])
        self.assertEqual(self.entries(), ([], []))
        self.safe()

    def test_a_world_writable_evidence_directory_is_refused_and_gives_no_line(self):
        """#407 residue: an existing evidence directory gets the `_private` test, not only `S_ISDIR`."""
        def loosen():
            self.quarantine.parent.mkdir(mode=0o700)
            os.chmod(self.quarantine.parent, 0o777)

        with self.after_backup(loosen):
            result = self.apply()
        self.gone()
        self.assertIn("recovery directory must be private, owned and unlinked", result["move_error"])
        self.assertEqual(result["move_commands"], [])
        self.assertEqual(result["moved"], [])
        self.assertTrue(all(path.exists() for path in self.files))
        self.assertEqual(sorted(self.quarantine.parent.iterdir()), [])

    def unlink_the_pairs(self):
        """The pairs go after the backup (which needs them) and before the re-plan, which then plans no move."""
        for path in self.files:
            path.unlink()

    def test_a_held_ending_lock_fails_the_apply_for_a_pair_less_run_too(self):
        """#407 residue: a released run with no pair at re-plan time still takes its ending lock."""
        ending = self.database.parent / "runner-ending" / f"{self.runs[1]}.lock"
        self.hold(ending)
        with self.after_backup(self.unlink_the_pairs):
            result = self.apply()
        self.gone()
        self.record_id()
        self.assertIn(str(ending), result["move_error"])
        self.assertIn("ending sequence", result["move_error"])
        self.assertEqual(result["moved"], [])
        self.assertEqual(result["move_commands"], [])
        self.assertFalse(self.quarantine.exists())

    def test_a_pair_persisted_after_the_plan_is_listed_under_the_ending_lock_and_moved(self):
        """The pair `Runner.finish` persists between the re-plan and the move is found under the ending lock."""
        rows = [dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (run,)).fetchone()) for run in self.runs]
        real = removal.transaction

        @contextmanager
        def wrapped(connection):
            with real(connection):
                yield connection
            for row in rows:
                journal.persist(self.database, row)

        with self.after_backup(self.unlink_the_pairs), mock.patch.object(removal, "transaction", wrapped):
            result = self.apply()
        self.gone()
        self.assertIsNone(result["move_error"])
        self.assertEqual(result["moved"], [str(path) for path in self.files])
        self.assertEqual(sorted(path.name for path in self.journal.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.quarantine.iterdir()),
                         sorted(["receipt.json", *(path.name for path in self.files)]))
        receipt = json.loads((self.quarantine / "receipt.json").read_text())
        self.assertEqual([entry["source"] for entry in receipt["files"]], [str(path) for path in self.files])
        self.assertEqual(self.entries(), ([], []))
        self.safe()

    def test_the_put_back_restores_the_rows_and_the_runner_files(self):
        record = self.apply()["record"]
        self.put_back(record)
        self.assertEqual(self.counts()["runner_run"], 2)
        self.assertEqual(sorted(path.name for path in self.journal.iterdir()), sorted(path.name for path in self.files))
        self.assertEqual(self.entries(), ([], []))
        self.safe()
        snapshot = backup.run(home=self.home, database=self.database, keep=None)
        self.assertEqual(snapshot.violations, [])

    def test_control_a_put_back_without_the_files_fails_every_backup(self):
        record = self.apply()["record"]
        self.put_back(record, move_files=False)
        self.assertEqual(self.entries(), ([], []))
        self.safe()
        with self.assertRaisesRegex(Exception, "runner row .* differs from the backup journal"):
            backup.run(home=self.home, database=self.database, keep=None)

    def test_the_quarantine_is_not_read_as_a_journal(self):
        other = create_item(self.db, kind="task", title="no runs", status="done")
        fingerprint = removal.plan_item(self.db, other, home=self.home)["fingerprint"]
        stray = uuid.uuid4().hex
        scratch = self.root / "scratch" / "sd.db"
        scratch.parent.mkdir()
        record = dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (self.runs[0],)).fetchone())
        record["id"] = stray
        journal.persist(scratch, record)
        evidence = self.database.parent / "runner-recovery-evidence" / f"removed-{'a' * 64}"
        evidence.parent.mkdir(mode=0o700)
        evidence.mkdir(mode=0o700)
        shutil.copy2(scratch.parent / "runner-journal" / f"{stray}.json", evidence / f"{stray}.json")
        removal.apply(self.db, "item", other, fingerprint=fingerprint, home=self.home, **WHO)
        self.assertEqual(self.entries(), ([], []))
        self.safe()
        snapshot = backup.run(home=self.home, database=self.database, keep=None)
        self.assertIn("runner-recovery-evidence", snapshot.configuration)

    def test_the_leftovers_stay_and_are_listed(self):
        directory = self.retained / str(self.works[0]) / "1"
        (directory / "archives" / "7").mkdir(parents=True)
        receipts = self.database.parent / "runner-reconciliation"; receipts.mkdir(mode=0o700)
        leftovers = [directory / "retention.json", directory / "kept.tar", directory / "archives" / "7" / "kept.tar",
                     directory / "archives" / "7" / "manifest.json", receipts / f"{'c' * 64}.json"]
        for path in leftovers[:-1]:
            path.write_text(f"bytes of {path.name}")
        leftovers[-1].write_text(json.dumps({"run": self.runs[0], "operation": "database-from-journal"}))
        digests = {path: self.digest(path) for path in leftovers}
        plan = removal.plan_repo(self.db, self.source, with_items=True, home=self.home)
        self.fingerprint = plan["fingerprint"]
        record = self.apply()["record"]
        self.assertEqual({path: self.digest(path) for path in leftovers}, digests)
        manifest = json.loads(self.db.execute("SELECT body FROM item WHERE id=?", (record,)).fetchone()[0])["text"]
        left = manifest.split("\nleft:\n", 1)[1].rstrip("\n").split("\n")
        for path in leftovers:
            self.assertIn(f"file {path}", left)
        self.assertEqual(self.entries(), ([], []))
        self.safe()


class HomeWithASpace(Home):
    prefix = "sd 754 "

    def test_every_line_splits_back_into_the_exact_paths(self):
        self.assertIn(" ", str(self.root))
        child = self.hold(self.pairs[0][1])
        lines = self.apply()["move_commands"]
        self.assertEqual(shlex.split(lines[0]), ["mkdir", "-m", "700", str(self.quarantine.parent)])
        self.assertEqual(shlex.split(lines[1]), ["mkdir", "-m", "700", str(self.quarantine)])
        for line, source in zip(lines[2:], self.files):
            parts = shlex.split(line)
            self.assertEqual((parts[2], parts[3], parts[8], parts[13]),
                             (str(source), str(self.quarantine / source.name), str(source), str(source)))
        self.kill(child)
        for line in lines:
            self.run_line(line)
        self.assertEqual(self.entries(), ([], []))


class Interrupts(Home):
    """C-48, C-57, C-63: what a `KeyboardInterrupt` gives at each point."""

    def test_an_interrupt_after_the_commit_returns_the_lines(self):
        with mock.patch.object(removal.os, "rename", side_effect=KeyboardInterrupt):
            result = self.apply()
        self.gone()
        self.assertEqual(result["move_error"], "KeyboardInterrupt")
        lines = result["move_commands"]
        self.assertEqual([shlex.split(line)[2] for line in lines if line.startswith("mv ")],
                         [str(path) for path in self.files])
        self.assertEqual([line for line in lines if line.startswith("mkdir ")], [])
        self.assertTrue((self.quarantine / "receipt.json").exists())
        self.assertTrue(all(path.exists() for path in self.files))

    def test_control_an_interrupt_before_the_commit_propagates(self):
        with mock.patch.object(reporting, "ingest", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.assertFalse(self.db.in_transaction)
        self.unchanged_but_state(1)

    def test_an_interrupt_as_commit_returns_is_a_commit(self):
        real = removal.transaction

        @contextmanager
        def wrapped(connection):
            with real(connection):
                yield connection
            raise KeyboardInterrupt

        with mock.patch.object(removal, "transaction", wrapped):
            result = self.apply()
        self.assertEqual(result["move_error"], "interrupted")
        self.assertEqual(len(result["move_commands"]), 6)
        fresh = connect(self.database); self.addCleanup(fresh.close)
        self.assertEqual(fresh.execute("SELECT count(*) FROM runner_run").fetchone()[0], 0)
        self.assertEqual(fresh.execute("SELECT count(*) FROM item WHERE id=?", (result["record"],)).fetchone()[0], 1)

    def test_an_interrupt_as_commit_returns_lists_a_pair_persisted_after_the_re_plan(self):
        """sd:968: the interrupted branch lists the pairs under the ending locks, as the move does.

        Its lines came from the re-plan's `plan["move"]`, so a pair
        `Runner.finish` persisted between the re-plan and the interrupt got
        no line and stayed in `runner-journal/` as a row-less entry.
        """
        rows = [dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (run,)).fetchone()) for run in self.runs]
        real = removal.transaction

        @contextmanager
        def wrapped(connection):
            with real(connection):
                yield connection
            for row in rows:
                journal.persist(self.database, row)
            raise KeyboardInterrupt

        with self.after_backup(lambda: [path.unlink() for path in self.files]), \
                mock.patch.object(removal, "transaction", wrapped):
            result = self.apply()
        self.gone()
        self.assertEqual((result["move_error"], result["moved"]), ("interrupted", []))
        lines = result["move_commands"]
        self.assertEqual(lines[:2], [f"mkdir -m 700 {shlex.quote(str(self.quarantine.parent))}",
                                     f"mkdir -m 700 {shlex.quote(str(self.quarantine))}"])
        self.assertEqual([shlex.split(line)[2] for line in lines[2:]], [str(path) for path in self.files])
        self.assertTrue(all(path.exists() for path in self.files))
        self.assertEqual(len(self.entries()[0]), 2)
        for line in lines:
            self.run_line(line)
        self.assertEqual(self.entries(), ([], []))
        self.safe()

    def test_an_interrupt_as_commit_returns_with_nothing_to_move_gives_no_line(self):
        """Copilot on #407: an item with no run has no quarantine path; the result still comes back."""
        other = create_item(self.db, kind="task", title="no runs", status="done")
        fingerprint = removal.plan_item(self.db, other, home=self.home)["fingerprint"]
        real = removal.transaction

        @contextmanager
        def wrapped(connection):
            with real(connection):
                yield connection
            raise KeyboardInterrupt

        with mock.patch.object(removal, "transaction", wrapped):
            result = removal.apply(self.db, "item", other, fingerprint=fingerprint, home=self.home, **WHO)
        self.assertEqual((result["move_error"], result["move_commands"], result["moved"]), ("interrupted", [], []))
        self.assertIsNone(self.db.execute("SELECT 1 FROM item WHERE id=?", (other,)).fetchone())
        self.assertEqual(self.entries(), ([], []))

    def test_control_an_interrupt_inside_the_block_rolls_back(self):
        real = removal.transaction

        @contextmanager
        def wrapped(connection):
            with real(connection):
                yield connection
                raise KeyboardInterrupt

        with mock.patch.object(removal, "transaction", wrapped), self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.unchanged_but_state(1)

    def test_the_round_5_probe_a_second_apply_after_a_put_back(self):
        """C-63: `ingest` refuses inside the transaction; the apply raises and prints nothing."""
        record = self.apply()["record"]
        self.put_back(record)
        self.before = self.counts()
        with mock.patch.object(removal, "_put_back", return_value=None):
            self.assertEqual(removal.plan_repo(self.db, self.source, with_items=True, home=self.home)["fingerprint"],
                             self.fingerprint)
            with self.assertRaisesRegex(Exception, "this report identity already has different evidence") as caught:
                self.apply()
        self.assertNotIsInstance(caught.exception, KeyboardInterrupt)
        self.assertFalse(hasattr(caught.exception, "move_commands"))
        self.unchanged_but_state(1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE external_id=?",
                                         (f"repo-remove:{self.fingerprint}",)).fetchone()[0], 1)

    def test_an_interrupt_beside_an_old_record_is_re_raised(self):
        """C-63: the old record's `external_id` alone does not make this apply a commit."""
        record = self.apply()["record"]
        self.put_back(record)
        self.before = self.counts()
        real = removal.plan_repo
        calls = []

        def second_raises(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with mock.patch.object(removal, "_put_back", return_value=None), \
                mock.patch.object(removal, "plan_repo", side_effect=second_raises), self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.assertEqual(len(calls), 2)
        self.unchanged_but_state(1)

    def test_a_second_apply_after_a_put_back_is_g6_before_the_backup(self):
        """C-64."""
        record = self.apply()["record"]
        self.put_back(record)
        snapshots = sorted((self.home / "Documents" / "sd-backups").iterdir())
        with mock.patch.object(backup, "run", wraps=backup.run) as run, \
                self.assertRaisesRegex(removal.RemovalRefused, "G6"):
            self.apply()
        self.assertEqual(run.call_count, 0)
        self.assertEqual(sorted((self.home / "Documents" / "sd-backups").iterdir()), snapshots)


class StopPoints(Home):
    """C-57, C-67, C-66: the three `stop()` checks, all before the commit, none after."""

    def test_before_control_gate(self):
        with mock.patch.object(removal.operations, "control_gate", wraps=removal.operations.control_gate) as gate, \
                mock.patch.object(backup, "run", wraps=backup.run) as run, \
                self.assertRaises(removal.RemovalInterrupted):
            self.apply(stop=lambda: True)
        self.assertEqual((gate.call_count, run.call_count), (0, 0))
        self.assertEqual(self.counts(), self.before)

    def test_after_the_backup(self):
        flag = []
        real = backup.run

        def run(**kwargs):
            snapshot = real(**kwargs)
            flag.append(True)
            return snapshot

        with mock.patch.object(backup, "run", side_effect=run), \
                mock.patch.object(reporting, "ingest", wraps=reporting.ingest) as ingest, \
                self.assertRaises(removal.RemovalInterrupted):
            self.apply(stop=lambda: bool(flag))
        self.assertEqual(ingest.call_count, 0)
        snapshots = sorted((self.home / "Documents" / "sd-backups").iterdir())
        self.assertEqual(len(snapshots), 1)
        self.assertTrue((snapshots[0] / "sd.db").exists())
        self.assertTrue((snapshots[0] / "runner-journal").is_dir())
        self.unchanged_but_state(1)

    def test_inside_the_transaction(self):
        flag = []
        real = reporting.ingest

        def ingest(*args, **kwargs):
            flag.append(True)
            return real(*args, **kwargs)

        with mock.patch.object(reporting, "ingest", side_effect=ingest) as patched, \
                self.assertRaises(removal.RemovalInterrupted):
            self.apply(stop=lambda: bool(flag))
        self.assertEqual(patched.call_count, 1)
        self.assertFalse(self.db.in_transaction)
        self.assertIsNone(self.db.execute("SELECT 1 FROM item WHERE source='cron-report'").fetchone())
        self.unchanged_but_state(1)

    def test_after_the_last_check_the_apply_commits_and_finishes_the_move(self):
        calls = []

        def stop():
            calls.append(True)
            return len(calls) > 3

        result = self.apply(stop=stop)
        self.gone()
        self.assertEqual(result["move_commands"], [])
        self.assertIsNone(result["move_error"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.entries(), ([], []))


NESTED = textwrap.dedent("""
    import os, signal, sys, threading
    from sd_db import removal
    original = threading.Condition.notify_all
    fired = []

    def once(self):
        if not fired:
            fired.append(True)
            os.kill(os.getpid(), signal.SIGTERM)
        return original(self)

    threading.Condition.notify_all = once
    names = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old = {number: signal.getsignal(number) for number in names}
    with removal.signal_stop() as stop:
        inside = {number: signal.getsignal(number) for number in names}
        print(all(inside[number] is not old[number] and callable(inside[number]) for number in names), flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
        print(stop(), flush=True)
    print(all(signal.getsignal(number) is old[number] for number in names), flush=True)
    """)


class SignalStop(unittest.TestCase):

    def test_a_nested_signal_does_not_hang_and_the_handlers_come_back(self):
        child = subprocess.Popen([sys.executable, "-c", NESTED], stdout=subprocess.PIPE, text=True)
        try:
            output, _ = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.kill(child.pid, signal.SIGKILL)
            child.wait()
            self.fail("signal_stop hung on a nested signal")
        self.assertEqual(child.returncode, 0, output)
        self.assertEqual(output.split(), ["True", "True", "True"])

    def test_the_old_handlers_come_back_after_an_exception(self):
        names = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        old = {number: signal.getsignal(number) for number in names}
        with self.assertRaises(RuntimeError):
            with removal.signal_stop() as stop:
                self.assertFalse(stop())
                self.assertTrue(all(signal.getsignal(number) is not old[number] for number in names))
                raise RuntimeError("out")
        self.assertEqual({number: signal.getsignal(number) for number in names}, old)


if __name__ == "__main__":
    unittest.main()
