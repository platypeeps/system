"""What the row records of a session: its notes, and a `start` entry's one cost row.

Criterion 1's record (`prd.md:606-607`): provider, started, ended, cost and
the worktree path on the row, and the session's followups, decisions and
proposals as `note` rows. Item B's 15.7, 15.13 and 15.16: the runner writes
no cost SQL of its own, a `start` session is charged one `run` row carrying
the total it reported at exit and no per-call row, and it runs to its end
uncapped.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db.testing.providers import ProviderDouble
from sd_runner import runtime, session_record

from . import test_runtime

RUNNER = Path(__file__).resolve().parents[1]
CLAUDE = {"provider": "claude", "vendor": "anthropic", "start": True, "bill": "anthropic", "reader": "claude-json"}


class Sessions(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("root", "db", "item", "config", "runner", "provider"):
            setattr(self, name, getattr(self.fixture, name))
        self.environment = {"PATH": os.environ["PATH"], "HOME": str(self.root)}

    def run_fixture(self, **extra):
        request = self.fixture.claim()
        return request, self.runner.execute(self.db, request, command=[sys.executable, str(self.provider)], environment=self.environment, **extra)

    def notes(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM note WHERE item=? AND session IS NOT NULL AND session != 'runner' ORDER BY id", (self.item,))]

    def costs(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM cost ORDER BY id")]

    def seed_registry_rows(self):
        """The `cost` row's foreign keys, and the attribution a claude session's commit carries."""
        self.db.execute("INSERT INTO bill (name, cost_basis) VALUES ('anthropic', 'subscription')")
        self.db.execute("INSERT INTO provider (name, enabled, author_rank) VALUES ('claude', 1, 1)")
        self.db.commit()
        self.provider.write_text(self.provider.read_text().replace("Authored-with: fixture/fixture", "Authored-with: claude/anthropic"))

    def test_row_records_provider_start_end_path_and_the_sessions_notes(self):
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "with open('.git/sd-notes.jsonl', 'a') as f:\n"
            "    f.write(json.dumps({'kind': 'followup', 'body': 'the fixture has no lint step yet'}) + '\\n')\n"
            "    f.write(json.dumps({'kind': 'decision', 'body': '  kept the file name  '}) + '\\n\\n')\n"
            "    f.write(json.dumps({'kind': 'proposal', 'body': 'split the module'}) + '\\n')\n"
            "    f.write(json.dumps({'kind': 'question', 'body': 'is 8767 fixed?'}) + '\\n')\n")
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(result["provider"], "fixture")
        self.assertEqual(result["vendor"], "fixture")
        self.assertEqual((result["start_step"], result["end_step"]), ("started", "released"))
        self.assertIsNotNone(result["created_at"])
        self.assertIsNotNone(result["released_at"])
        self.assertTrue(result["work_path"] and result["retained_path"], result)
        notes = self.notes()
        self.assertEqual([(n["kind"], n["body"], n["session"]) for n in notes],
                         [("followup", "the fixture has no lint step yet", result["id"]), ("decision", "kept the file name", result["id"]),
                          ("proposal", "split the module", result["id"]), ("question", "is 8767 fixed?", result["id"])])
        self.assertIsNone(notes[0]["resolved_at"])
        # A `url`-less fixture is not a `start` entry: no cost row is written for it.
        self.assertEqual(self.costs(), [])

    def test_notes_are_kept_when_the_session_fails(self):
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "open('.git/sd-notes.jsonl', 'w').write(json.dumps({'kind': 'followup', 'body': 'left off at step 2'}) + '\\n')\n"
            "raise SystemExit(2)\n")
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn("provider exited 2", result["detail"])
        self.assertEqual([(n["kind"], n["body"]) for n in self.notes()], [("followup", "left off at step 2")])

    def test_a_malformed_note_line_blocks_rather_than_dropping_it(self):
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "open('.git/sd-notes.jsonl', 'w').write(json.dumps({'kind': 'followup', 'body': 'fine'}) + '\\n{\"kind\": \"comment\", \"body\": \"x\"}\\n')\n")
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn(".git/sd-notes.jsonl line 2", result["detail"])
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "blocked")
        self.assertEqual(self.notes(), [], "nothing is filed from a file the runner could not read whole")

    def test_start_session_is_charged_one_run_row_with_its_exit_total(self):
        """15.13 and 15.16: three calls, one row, the session's own total, run to its end."""
        self.seed_registry_rows()
        with ProviderDouble() as double:
            url = double.base_url("kimi") + "/chat/completions"
            self.provider.write_text(self.provider.read_text() + f"import json, urllib.request\n"
                f"for turn in range(3):\n"
                f"    req = urllib.request.Request({url!r}, data=json.dumps({{'model': 'm', 'messages': []}}).encode(), headers={{'Content-Type': 'application/json'}})\n"
                f"    print(json.dumps({{'type': 'assistant', 'turn': turn, 'reply': json.load(urllib.request.urlopen(req))['id']}}))\n"
                f"print('a stray non-JSON line the reader must skip')\n"
                f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 3, 'total_cost_usd': 1.2345,\n"
                f"                  'usage': {{'input_tokens': 123456, 'output_tokens': 7890, 'cache_read_input_tokens': 5}}}}))\n")
            request, result = self.run_fixture(provider=CLAUDE)
            self.assertEqual(len(double.calls), 3, "the session made three calls of its own")
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(result["provider"], "claude")
        rows = self.costs()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual((row["source"], row["provider"], row["bill"], row["role"], row["repo"]),
                         ("run", "claude", "anthropic", "author", str(self.fixture.checkout)))
        self.assertEqual((row["assignment"], row["pass"], row["call_id"]), (request["id"], result["id"], f"session:{result['id']}"))
        self.assertEqual((row["tokens_in"], row["tokens_out"], row["usd"]), (123456, 7890, 1.2345))
        # The item's cost is the sum of its rows: this one.
        total = self.db.execute("SELECT SUM(usd) AS usd FROM cost WHERE assignment=?", (request["id"],)).fetchone()["usd"]
        self.assertEqual(total, 1.2345)

    def test_start_session_that_reports_nothing_still_gets_its_row_with_null_numbers(self):
        self.seed_registry_rows()
        request, result = self.run_fixture(provider=CLAUDE)
        self.assertEqual(result["outcome"], "done", result)
        rows = self.costs()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["call_id"], rows[0]["tokens_in"], rows[0]["tokens_out"], rows[0]["usd"]), (f"session:{result['id']}", None, None, None))

    def test_a_session_commits_as_its_provider(self):
        # sd:2544: the pack's commit-msg hook writes `Authored-with:` from
        # SD_AUTHOR, and nothing the runner started carried it.
        self.provider.write_text(self.provider.read_text() + "import os\n"
            "open('.git/sd-author', 'w').write(os.environ.get('SD_AUTHOR', 'unset'))\n")
        request, result = self.run_fixture()
        self.assertEqual((Path(result["retained_path"]) / ".git/sd-author").read_text(), "fixture")

    def test_a_session_lost_after_it_ran_is_still_recorded(self):
        # sd:1221. `_response` raises on a cancellation, an expired deadline or
        # a lost supervisor, and the record below it never ran: the session had
        # already spent, and the ending records no usage.
        self.seed_registry_rows()
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.5, 'usage': {'input_tokens': 10, 'output_tokens': 2}}))\n"
            "open('.git/sd-notes.jsonl', 'w').write(json.dumps({'kind': 'followup', 'body': 'stopped at step 2'}) + '\\n')\n")
        acknowledged = runtime.Runner.action

        def lost(runner, connection, child, request, action, deadline, **extra):
            result = acknowledged(runner, connection, child, request, action, deadline, **extra)
            if action == "provider":
                raise store.RunnerRefused("supervisor lost after the session acknowledged")
            return result

        with patch.object(runtime.Runner, "action", lost):
            request, result = self.run_fixture(provider=CLAUDE)
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn("supervisor lost", result["detail"])
        self.assertEqual([(r["usd"], r["tokens_in"]) for r in self.costs()], [(0.5, 10)])
        self.assertEqual([(n["kind"], n["body"]) for n in self.notes()], [("followup", "stopped at step 2")])

    def interrupt_provider(self, hold):
        """Run a session that spent and left a note, then `hold` and raise as a restore or takeover does."""
        self.seed_registry_rows()
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.5, 'usage': {'input_tokens': 10, 'output_tokens': 2}}))\n"
            "open('.git/sd-notes.jsonl', 'w').write(json.dumps({'kind': 'followup', 'body': 'stopped at step 2'}) + '\\n')\n")
        acknowledged = runtime.Runner.action

        def interrupted(runner, connection, child, request, action, deadline, **extra):
            result = acknowledged(runner, connection, child, request, action, deadline, **extra)
            if action == "provider":
                hold(connection, request)
                raise store.RunnerRefused("database restore interrupted this owned attempt")
            return result

        with patch.object(runtime.Runner, "action", interrupted):
            request, result = self.run_fixture(provider=CLAUDE)
        self.assertTrue(result.get("recovery_hold"), result)
        # Nothing reaches the store through the hold; the clone keeps both files.
        self.assertEqual(self.costs(), [])
        self.assertEqual(self.notes(), [])
        clone = Path(result["work_path"])
        self.assertIn("stopped at step 2", (clone / session_record.NOTES_FILE).read_text())
        self.assertIn("total_cost_usd", (clone / ".git/sd-provider.log").read_text())

    def test_a_restore_holds_the_interrupted_sessions_record(self):
        # sd:1270. The handler sd:1221 added wrote notes and cost while a
        # restore was starting, which the ending's recovery guard then held.
        def restore(connection, request):
            (self.config.database.parent / "runner-restore-intent.json").write_text("{}")
        self.interrupt_provider(restore)

    def test_an_ownership_change_holds_the_interrupted_sessions_record(self):
        def takeover(connection, request):
            connection.execute("UPDATE runner_run SET owner = 'another-owner' WHERE id = ?", (request["run"]["id"],))
            connection.commit()
        self.interrupt_provider(takeover)

    def test_an_unreadable_notes_file_still_charges_the_session(self):
        # sd:1221. The notes were read before the cost path, so a file the
        # runner refused took the session's spend down with it.
        self.seed_registry_rows()
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.75, 'usage': {'input_tokens': 11, 'output_tokens': 3}}))\n"
            "open('.git/sd-notes.jsonl', 'w').write('{\"kind\": \"comment\", \"body\": \"x\"}\\n')\n")
        request, result = self.run_fixture(provider=CLAUDE)
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn(".git/sd-notes.jsonl line 1", result["detail"])
        self.assertEqual([(r["usd"], r["tokens_in"]) for r in self.costs()], [(0.75, 11)])
        self.assertEqual(self.notes(), [], "nothing is filed from a file the runner could not read whole")

    def test_notes_and_cost_are_one_commit(self):
        # sd:1221. Two commits left a runner that died between them with the
        # notes filed and no cost; a failing cost write now takes both back.
        self.seed_registry_rows()
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.5, 'usage': {'input_tokens': 10, 'output_tokens': 2}}))\n"
            "open('.git/sd-notes.jsonl', 'w').write(json.dumps({'kind': 'followup', 'body': 'filed with the cost or not at all'}) + '\\n')\n")
        with patch.object(session_record.store, "record_session_cost", side_effect=store.RunnerRefused("cost row refused")):
            request, result = self.run_fixture(provider=CLAUDE)
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn("cost row refused", result["detail"])
        self.assertEqual(self.costs(), [])
        self.assertEqual(self.notes(), [])

    def test_stopped_start_session_is_still_charged(self):
        self.seed_registry_rows()
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.5, 'usage': {'input_tokens': 10, 'output_tokens': 2}}))\n"
            "open('.git/sd-stop.json', 'w').write(json.dumps({'kind': 'failing test', 'detail': 'cannot fix tests/test_x.py'}))\n")
        request, result = self.run_fixture(provider=CLAUDE)
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertEqual([(r["usd"], r["tokens_in"]) for r in self.costs()], [(0.5, 10)])


class Readers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "sd-provider.log"

    def test_last_result_envelope_wins_and_noise_is_skipped(self):
        self.log.write_text('{"type": "system", "usage": {"input_tokens": 1}}\n'
                            '{"type": "result", "total_cost_usd": 0.1, "usage": {"input_tokens": 1, "output_tokens": 1}}\n'
                            'warning: something on stderr\n'
                            '{"type": "result", "total_cost_usd": 2.5, "usage": {"input_tokens": 300, "output_tokens": 40}}\n'
                            '{not json\n')
        self.assertEqual(session_record.read_usage(self.log, "claude-json"),
                         {"found": True, "tokens_in": 300, "tokens_out": 40, "usd": 2.5})
        self.assertEqual(session_record.read_usage(self.log, "codex-json")["usd"], 2.5)

    def test_no_envelope_no_reader_or_no_log_is_not_found(self):
        empty = {"found": False, "tokens_in": None, "tokens_out": None, "usd": None}
        self.log.write_text("plain provider chatter\n")
        self.assertEqual(session_record.read_usage(self.log, "claude-json"), empty)
        self.assertEqual(session_record.read_usage(self.log, None), empty)
        self.assertEqual(session_record.read_usage(self.log, "unknown-reader"), empty)
        self.assertEqual(session_record.read_usage(self.log.with_name("absent.log"), "claude-json"), empty)

    def test_unusable_numbers_are_null_not_wrong(self):
        self.log.write_text('{"type": "result", "total_cost_usd": "free", "usage": {"input_tokens": -1, "output_tokens": 2.5}}\n')
        self.assertEqual(session_record.read_usage(self.log, "claude-json"), {"found": True, "tokens_in": None, "tokens_out": None, "usd": None})

    def test_note_file_limits(self):
        clone = Path(self.tmp.name)
        (clone / ".git").mkdir()
        self.assertEqual(session_record.read_notes(clone), [])
        notes = clone / session_record.NOTES_FILE
        notes.write_text("\n".join(json.dumps({"kind": "followup", "body": "n"}) for _ in range(session_record.NOTE_COUNT_LIMIT + 1)))
        with self.assertRaisesRegex(store.RunnerRefused, "more than"):
            session_record.read_notes(clone)
        notes.write_text(json.dumps({"kind": "followup", "body": "x" * (session_record.NOTE_BODY_LIMIT + 50)}) + "\n")
        self.assertEqual(len(session_record.read_notes(clone)[0]["body"]), session_record.NOTE_BODY_LIMIT)
        notes.write_text(json.dumps({"kind": "followup", "body": "   "}) + "\n")
        with self.assertRaisesRegex(store.RunnerRefused, "line 1"):
            session_record.read_notes(clone)

    def test_a_linked_notes_file_is_refused(self):
        # sd:1221. `is_file()` follows the link, so a session could have made
        # the runner file an operator's file as `note` rows.
        clone = Path(self.tmp.name) / "clone"
        (clone / ".git").mkdir(parents=True)
        outside = Path(self.tmp.name) / "operator-notes.jsonl"
        outside.write_text(json.dumps({"kind": "followup", "body": "private"}) + "\n")
        (clone / session_record.NOTES_FILE).symlink_to(outside)
        with self.assertRaisesRegex(store.RunnerRefused, "is a link"):
            session_record.read_notes(clone)

    def test_malformed_note_bytes_refuse_rather_than_being_altered(self):
        # sd:1221. `errors="replace"` turned invalid UTF-8 into U+FFFD and let
        # the note through changed, which the refusal contract forbids.
        clone = Path(self.tmp.name) / "bytes"
        (clone / ".git").mkdir(parents=True)
        (clone / session_record.NOTES_FILE).write_bytes(b'{"kind": "followup", "body": "caf\xe9"}\n')
        with self.assertRaisesRegex(store.RunnerRefused, "not UTF-8"):
            session_record.read_notes(clone)


class NoCostSql(unittest.TestCase):
    """Item B's 15.7: a grep of the runner for a cost insert returns nothing.

    The library is the one writer of `cost`. The runner's own sources may
    name the table in prose, never in SQL, and reach `record_cost` only
    through `sd_db.runner.record_session_cost`.
    """

    SQL = re.compile(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+cost\b", re.IGNORECASE)

    def sources(self):
        files = sorted((RUNNER / "sd_runner").glob("*.py")) + [RUNNER / "runner.sh"]
        self.assertGreater(len(files), 5, files)
        return files

    def test_runner_writes_no_cost_sql(self):
        hits = [f"{path.relative_to(RUNNER)}:{number}: {line.strip()}" for path in self.sources()
                for number, line in enumerate(path.read_text().splitlines(), 1) if self.SQL.search(line)]
        self.assertEqual(hits, [])

    def test_record_cost_is_reached_only_through_the_library(self):
        direct = [str(path.relative_to(RUNNER)) for path in self.sources() if re.search(r"\brecord_cost\b", path.read_text())]
        self.assertEqual(direct, [])
        callers = [str(path.relative_to(RUNNER)) for path in self.sources() if "record_session_cost" in path.read_text()]
        self.assertEqual(callers, ["sd_runner/session_record.py"])


if __name__ == "__main__":
    unittest.main()
