"""The trace corpus: what each call sent and got back, kept so a run can be
redone and relabelled from stored data (sd:2790).

Every corpus here is a temp folder of the case's own; `test_jev`'s `env`
pins `JEV_CORPUS_DIR`, so no case writes to the operator's.

The promises checked:

* a call stores the request as sent, after redaction, the whole response,
  the flags that shaped the printed answer, and its ledger row's id;
* `jev`'s record, the baseline's and each arm's share one call id;
* a call that sent nothing stores no request;
* every content field is redacted, a local-only call's included, and the
  secret scanner's is stored as SHA-256 digests and never as text;
* `JEV_CORPUS=0` stores nothing, and a corpus that cannot be written, or
  whose lock is held, costs the caller nothing.
"""

import hashlib
import json
import os
import stat
import fcntl
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import jev
import jev_corpus

from .test_jev import Stub
from .test_jev_compare import Arm, CompareCase
from .test_jev_metering import SENTINEL, MeteringCase

#: A credential `redact` knows by its shape, with no pattern file needed.
SECRET = "ghp_" + "c" * 36


def records(folder) -> list:
    out = []
    for path in sorted(Path(folder).glob("*.jsonl")):
        out += [json.loads(line) for line in path.read_text().splitlines()]
    return out


class TheModule(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "corpus"

    def test_a_record_is_one_line_in_a_private_file_per_utc_day(self):
        said = jev_corpus.append({"arm": "jev"}, {"JEV_CORPUS_DIR": str(self.folder)})
        self.assertEqual(said, jev_corpus.WRITTEN)
        files = list(self.folder.iterdir())
        self.assertEqual([f.name for f in files],
                         [time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl"])
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        found, = records(self.folder)
        self.assertEqual((found["arm"], found["schema"]), ("jev", jev_corpus.SCHEMA))
        self.assertRegex(found["id"], r"^[0-9a-f]{32}$")
        self.assertTrue(found["time"].endswith("+00:00"))

    def test_every_off_word_stores_nothing(self):
        for word in ("0", "off", "FALSE", "no", "disabled"):
            with self.subTest(word=word):
                said = jev_corpus.append({}, {"JEV_CORPUS": word,
                                              "JEV_CORPUS_DIR": str(self.folder)})
                self.assertEqual(said, jev_corpus.SWITCHED_OFF)
                self.assertFalse(self.folder.exists())

    def test_a_folder_that_cannot_be_made_is_a_word_not_an_exception(self):
        self.folder.parent.joinpath("file").write_text("x")
        said = jev_corpus.append({}, {"JEV_CORPUS_DIR": str(self.folder.parent / "file")})
        self.assertEqual(said, jev_corpus.FAILED)

    def test_an_existing_folder_and_file_are_held_to_the_private_modes(self):
        self.folder.mkdir(mode=0o755)
        day = self.folder / (time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
        day.touch(mode=0o644)
        os.chmod(self.folder, 0o755)
        os.chmod(day, 0o644)
        said = jev_corpus.append({}, {"JEV_CORPUS_DIR": str(self.folder)})
        self.assertEqual(said, jev_corpus.WRITTEN)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(day.stat().st_mode), 0o600)

    def test_a_symlinked_file_or_folder_is_refused(self):
        target = self.folder.parent / "elsewhere"
        target.mkdir()
        self.folder.mkdir()
        day = self.folder / (time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
        day.symlink_to(target / "kept.jsonl")
        self.assertEqual(jev_corpus.append({}, {"JEV_CORPUS_DIR": str(self.folder)}),
                         jev_corpus.FAILED)
        linked = self.folder.parent / "linked"
        linked.symlink_to(target)
        self.assertEqual(jev_corpus.append({}, {"JEV_CORPUS_DIR": str(linked)}),
                         jev_corpus.FAILED)
        self.assertEqual(list(target.iterdir()), [])

    def test_a_fifo_in_the_file_s_place_is_refused_without_waiting(self):
        self.folder.mkdir()
        os.mkfifo(self.folder / (time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl"))
        began = time.monotonic()
        self.assertEqual(jev_corpus.append({}, {"JEV_CORPUS_DIR": str(self.folder)}),
                         jev_corpus.FAILED)
        self.assertLess(time.monotonic() - began, 1.0)

    def test_a_short_write_still_leaves_a_whole_line(self):
        real = os.write
        with mock.patch.object(jev_corpus.os, "write",
                               side_effect=lambda fd, data: real(fd, data[:1])):
            said = jev_corpus.append({"arm": "jev"}, {"JEV_CORPUS_DIR": str(self.folder)})
        self.assertEqual(said, jev_corpus.WRITTEN)
        found, = records(self.folder)
        self.assertEqual(found["arm"], "jev")

    def test_a_write_that_fails_midway_leaves_no_fragment(self):
        env = {"JEV_CORPUS_DIR": str(self.folder)}
        jev_corpus.append({"arm": "first"}, env)
        real, calls = os.write, []

        def full_disk(fd, data):
            calls.append(fd)
            if len(calls) > 1:
                raise OSError(28, "No space left on device")
            return real(fd, data[:5])

        with mock.patch.object(jev_corpus.os, "write", side_effect=full_disk):
            self.assertEqual(jev_corpus.append({"arm": "second"}, env), jev_corpus.FAILED)
        jev_corpus.append({"arm": "third"}, env)
        self.assertEqual([r["arm"] for r in records(self.folder)], ["first", "third"])

    def test_a_tail_a_killed_writer_left_is_cut_before_the_next_line(self):
        env = {"JEV_CORPUS_DIR": str(self.folder)}
        jev_corpus.append({"arm": "first"}, env)
        day, = self.folder.iterdir()
        with day.open("a") as fh:
            fh.write('{"arm": "torn", "sch')
        jev_corpus.append({"arm": "next"}, env)
        self.assertEqual([r["arm"] for r in records(self.folder)], ["first", "next"])

    def test_a_held_lock_drops_the_record_within_the_bound(self):
        env = {"JEV_CORPUS_DIR": str(self.folder)}
        jev_corpus.append({"arm": "first"}, env)
        day, = self.folder.iterdir()
        holder = os.open(day, os.O_WRONLY)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        began = time.monotonic()
        said = jev_corpus.append({"arm": "second"}, env)
        took = time.monotonic() - began
        self.assertEqual(said, jev_corpus.FAILED)
        self.assertLess(took, jev_corpus.LOCK_WAIT + 1.0)
        fcntl.flock(holder, fcntl.LOCK_UN)
        jev_corpus.append({"arm": "third"}, env)
        self.assertEqual([r["arm"] for r in records(self.folder)], ["first", "third"])

    def test_the_default_folder_is_under_home(self):
        self.assertEqual(jev_corpus.directory({"HOME": "/home/example"}),
                         "/home/example/.local/share/sd/jev-corpus")


class ACall(MeteringCase):
    def stored(self):
        return records(self.corpus)

    def test_a_call_stores_what_it_sent_what_came_back_and_its_ledger_row(self):
        code, out = self.run_main(["choice", "which route?", "--criteria", "desk,phone",
                                   "--unsure-below", "0.5", "--caller", "local-notify",
                                   "--stage", "JEV_NOTIFY"], stdin="the state")
        self.assertEqual((code, out), (0, "desk\n"))
        found, = self.stored()
        self.assertEqual(found["request"], Stub.seen[-1]["payload"])
        self.assertEqual(found["response"]["answers"]["answer"]["probabilities"],
                         {"desk": 0.0, "phone": 0.0})
        self.assertEqual((found["arm"], found["caller"], found["stage"], found["model"],
                          found["outcome"], found["printed"]),
                         ("jev", "local-notify", "JEV_NOTIFY", "jev-stub", "ok", "desk"))
        self.assertEqual(found["settings"]["unsure_below"], 0.5)
        # The criteria are kept once, as sent, in the request.
        self.assertNotIn("criteria", found["settings"])
        self.assertEqual(found["ledger"], self.only()["id"])
        self.assertRegex(found["call"], r"^[0-9a-f]{16}$")

    def test_the_stored_request_is_the_redacted_one(self):
        secret = "ghp_" + "a" * 36
        self.run_main(["noul", "is it?"], stdin=f"token {secret} here")
        found, = self.stored()
        self.assertEqual(found["request"]["state"], "token [REDACTED] here")
        self.assertNotIn(secret, json.dumps(found))

    def test_a_paired_call_stores_both_arms_under_one_call(self):
        self.run_main(["noul", "is it?", "--gate", "0.5", "--baseline", "no",
                       "--baseline-ms", "4"])
        mine, theirs = self.stored()
        self.assertEqual((mine["arm"], theirs["arm"]), ("jev", "baseline"))
        self.assertEqual(mine["call"], theirs["call"])
        self.assertEqual(mine["pair"], theirs["pair"])
        self.assertEqual((mine["baseline"], theirs["baseline"]), ("no", "no"))
        rows = {row["arm"]: row["id"] for row in self.rows()}
        self.assertEqual((mine["ledger"], theirs["ledger"]), (rows["jev"], rows["baseline"]))

    def test_no_text_the_caller_gave_is_stored_unredacted(self):
        secret = "ghp_" + "b" * 36
        self.run_main(["noul", f"is {secret} live?", "--fallback", secret,
                       "--baseline", secret, "--gate", "0.5"],
                      stdin="state", JEV_ENABLED="0")
        self.run_main(["noul", f"is {secret} live?", "--fallback", secret,
                       "--model", f"jev-{secret}"])
        stored = self.stored()
        self.assertEqual(len(stored), 3)
        self.assertNotIn(secret, json.dumps(stored))
        self.assertIn("[REDACTED]", stored[0]["fallback"])
        self.assertEqual(stored[2]["settings"]["model"], "jev-[REDACTED]")

    def test_printed_is_what_reached_stdout_and_the_judgment_is_the_answer(self):
        code, out = self.run_main(["noul", "is it?", "--shadow", "no"])
        self.assertEqual((code, out), (0, "no\n"))
        code, out = self.run_main(["noul", "is it?", "--shadow", "yes"], JEV_ENABLED="0")
        self.assertEqual((code, out), (0, "yes\n"))
        self.run_main(["noul", "is it?"])
        judged, failed, live = [r for r in self.stored() if r["arm"] == "jev"]
        self.assertEqual((judged["printed"], judged["answer"]), ("no", "0.97"))
        self.assertEqual((failed["printed"], failed["answer"]), ("yes", None))
        self.assertEqual((live["printed"], live["answer"]), ("0.97", "0.97"))

    def test_a_call_that_sent_nothing_stores_no_request(self):
        code, out = self.run_main(["noul", "is it?", "--fallback", "yes"], JEV_ENABLED="0")
        self.assertEqual((code, out), (0, "yes\n"))
        found, = self.stored()
        self.assertIsNone(found["request"])
        self.assertEqual((found["outcome"], found["cause"], found["fallback"]),
                         ("fallback", "switched-off", "yes"))

    def test_switched_off_it_stores_nothing_and_the_ledger_still_gets_its_row(self):
        self.run_main(["noul", "is it?"], JEV_CORPUS="0")
        self.assertEqual(list(self.corpus.iterdir()), [])
        self.only()

    def test_a_corpus_that_cannot_be_written_changes_nothing_a_caller_sees(self):
        blocked = self.corpus / "a-file"
        blocked.write_text("x")
        self.assertEqual(self.run_main(["noul", "is it?"], JEV_CORPUS_DIR=str(blocked)),
                         (0, "0.97\n"))
        self.only()


class LocalContent(CompareCase):
    """A local-only call sends its text unredacted, and Kev can echo it. The
    corpus copy is redacted, or for the secret scanner hashed, field by
    field: instructions, criteria, state and response alike."""

    state = f"token {SECRET} here"

    def planted(self, stage):
        """Two calls with a credential in every content field: noul's
        instructions, state and Kev's echo, and choice's criteria."""
        for path in self.corpus.glob("*"):
            path.unlink()
        Arm.kev_answer = {"type": "noul", "noul": 0.81, "echo": self.state}
        code, out = self.run_main(["noul", f"is {SECRET} live?", "--local-only",
                                   "--stage", stage, "--model", f"kev-{SECRET}"],
                                  stdin=self.state)
        self.assertEqual((code, out), (0, "0.81\n"))
        Arm.kev_answer = None
        code, _ = self.run_main(["choice", "where?", "--local-only", "--stage", stage,
                                 "--criteria", f"desk=near {SECRET},phone=elsewhere"],
                                stdin=self.state)
        self.assertEqual(code, 0)
        # Kev did get it, so each check below proves a filter, not an absence.
        self.assertIn(SECRET, str(Arm.seen))
        text = "".join(p.read_text() for p in self.corpus.glob("*.jsonl"))
        self.assertNotIn(SECRET, text)
        return records(self.corpus)

    def test_a_local_only_call_is_redacted_and_stays_replayable(self):
        noul, choice = self.planted("JEV_DOCS")
        self.assertEqual(noul["request"]["state"], "token [REDACTED] here")
        self.assertEqual(noul["request"]["questions"]["answer"]["instructions"],
                         "is [REDACTED] live?")
        self.assertEqual(noul["response"]["answers"]["answer"]["echo"],
                         "token [REDACTED] here")
        self.assertEqual(choice["request"]["questions"]["answer"]["criteria"]["desk"],
                         "near [REDACTED]")
        self.assertEqual(choice["printed"], "phone")
        self.assertEqual(noul["settings"]["model"], "kev-[REDACTED]")

    def test_the_secret_scanner_stores_only_digests(self):
        noul, choice = self.planted("JEV_SECRET_SCAN")
        for found in (noul, choice):
            for field in jev.CORPUS_CONTENT:
                value = found.get(field)
                if value is not None:
                    self.assertEqual(sorted(value), ["sha256"] if field != "request"
                                     else ["sha256", "state_sha256"], field)
        self.assertEqual(noul["request"]["state_sha256"],
                         hashlib.sha256(self.state.encode()).hexdigest())
        self.assertEqual(noul["settings"]["model"],
                         {"sha256": hashlib.sha256(f"kev-{SECRET}".encode()).hexdigest()})
        self.assertEqual(noul["settings"]["gate"], None)

    def test_a_named_stage_is_hashed_on_any_path(self):
        said = []
        record = {"stage": "JEV_SECRET_SCAN", "local": False,
                  "request": {"state": {"hit": SENTINEL}},
                  "prompts": {"q": {"user": SENTINEL}}}
        saved = jev.jev_corpus.append
        jev.jev_corpus.append = lambda rec, env: said.append(rec) or "written"
        try:
            jev.to_corpus(record, {})
        finally:
            jev.jev_corpus.append = saved
        self.assertNotIn(SENTINEL, json.dumps(said))
        self.assertEqual(said[0]["request"]["state_sha256"], hashlib.sha256(
            json.dumps({"hit": SENTINEL}, separators=(",", ":")).encode()).hexdigest())


class TheArms(CompareCase):
    def test_each_arm_stores_its_request_and_response_under_the_call(self):
        self.run_main(["noul", "is it?"], stdin="the state",
                      JEV_COMPARE_HAIKU_VIA="anthropic",
                      JEV_COMPARE_ANTHROPIC_KEY="test-key")
        rows = self.by_arm(self.wait_rows(3))
        deadline = time.monotonic() + 20
        while len(records(self.corpus)) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        found = {r["arm"]: r for r in records(self.corpus)}
        self.assertEqual(sorted(found), ["haiku", "jev", "kev"])
        self.assertEqual(len({r["call"] for r in found.values()}), 1)
        for arm in found:
            self.assertEqual(found[arm]["ledger"], rows[arm]["id"])
        kev_sent, = [s["body"] for s in Arm.seen if s["path"] == "/v1/systemone"]
        self.assertEqual(found["kev"]["request"], kev_sent)
        self.assertEqual(found["kev"]["response"]["answers"]["answer"]["noul"], 0.81)
        haiku = found["haiku"]
        self.assertIn("the state", haiku["prompts"]["answer"]["user"])
        self.assertEqual(json.loads(haiku["response"]["answer"]["reply"]), {"probability": 0.3})
        self.assertEqual(haiku["request"], found["jev"]["request"])


class TheEnvFile(MeteringCase):
    """`jev.sh` keeps an exported corpus setting over <config>/jev/.env."""

    def test_an_exported_folder_beats_the_env_file(self):
        from .test_jev import TestEnvFile
        helper = TestEnvFile("test_the_config_env_is_found_through_the_symlink")
        helper.url, helper.switch, helper.corpus = self.url, self.switch, self.corpus
        elsewhere = Path(tempfile.mkdtemp())
        link = helper.copy(f"TYPESAFE_API_KEY=k\nJEV_CORPUS_DIR={elsewhere}\n")
        self.addCleanup(helper.doCleanups)
        here = Path(__file__).resolve().parent.parent / "jev_corpus.py"
        (link.resolve().parent / "jev_corpus.py").write_bytes(here.read_bytes())
        done = helper.run_link(link, ["noul", "q"], {})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(records(self.corpus)), 1)
        self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
