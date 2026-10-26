"""`doctor gate` and the hub-ready lines it and the backups feed (sd:5986, slice 2).

docs/work/2026-10-25-hub-failover-full-role/design.md, "Weekly gate run",
"Readiness inputs" and the failure table's weekly gate and backup age rows.
`sd gate run`, `make` and `git` are stubs: no check runs and no gate slot is
taken. The job's stamp is written here as cron-jobs.sh writes it. The backup
snapshots are the pinned pack's own `sd-db backup` runs, taken at a set time.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from tests.test_backbone import PACK, ROOT, SCRIPT, write_stub
from tests.test_promote import GIT_STUB, SD_STUB, PromoteCase

# test_backbone puts the pinned pack's lib/ on the path.
from sd_db.backup import run as take_backup  # noqa: E402
from sd_db.migrate import initialise  # noqa: E402

SYSTEM_HEAD = "1" * 40
PACK_HEAD = "2" * 40
MOVED_HEAD = "3" * 40

# `sd gate run`: logs the call, then runs the gated command, or exits with
# STUB_GATE_system / STUB_GATE_pack; `kill` kills machine-setup.sh, the caller.
SD_GATE = r'''
if [ "$1 $2" = "gate run" ]; then
  echo "sd $*" >> "$STUB_LOG"
  while [ "$1" != -- ]; do shift; done; shift
  case "$5" in "$SD_PACK_ROOT") which=pack ;; *) which=system ;; esac
  eval "rc=\${STUB_GATE_$which:-run}"
  case $rc in
    run) exec "$@" ;;
    kill) kill -KILL "$PPID"; exit 137 ;;
    *) exit "$rc" ;;
  esac
fi
'''

# make -C <dir> check: exits STUB_MAKE_<which>; STUB_MOVE=<which> moves that
# checkout's head while its check runs.
MAKE_STUB = r'''
echo "make $*" >> "$STUB_LOG"
case "$2" in "$SD_PACK_ROOT") which=pack ;; *) which=system ;; esac
[ "${STUB_MOVE:-}" != "$which" ] || echo "$STUB_MOVED_HEAD" > "$STUB_HEADS/$which"
eval "exit \${STUB_MAKE_$which:-0}"
'''

# git: the two heads from files while STUB_HEADS is set, so a check can move one.
GIT_HEADS = r'''
if [ -n "${STUB_HEADS:-}" ]; then
  case "$*" in
    "-C $SD_PACK_ROOT rev-parse HEAD") cat "$STUB_HEADS/pack"; exit ;;
    *" rev-parse HEAD") cat "$STUB_HEADS/system"; exit ;;
  esac
fi
'''

# mv: STUB_MV_KILL kills machine-setup.sh between the record's temp file and its rename.
MV_STUB = '[ -z "${STUB_MV_KILL:-}" ] || { kill -KILL "$PPID"; exit 1; }\nexec /bin/mv "$@"\n'


def utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


class GateCase(PromoteCase):
    def setUp(self):
        super().setUp()
        f = self.fixture
        # The pack's bin/sd, as a launchd job finds it without PATH.
        write_stub(f.pack / "bin", "sd", SD_GATE + SD_STUB)
        write_stub(f.stubs, "git", GIT_HEADS + GIT_STUB)
        write_stub(f.stubs, "make", MAKE_STUB)
        self.heads = self.tmp / "heads"
        self.heads.mkdir()
        (self.heads / "system").write_text(SYSTEM_HEAD + "\n")
        (self.heads / "pack").write_text(PACK_HEAD + "\n")
        self.gate_record.unlink()
        self.gate_stamp.parent.mkdir(parents=True)

    def run_gate(self, expect=0, **extra):
        """One weekly job run: its stamp before and after, as cron-jobs.sh writes it."""
        started = utc(now())
        self.gate_stamp.write_text(f"started={started}\n")
        result = self.fixture.run("doctor", "gate", **self.promote_env(
            STUB_HEADS=self.heads, STUB_MOVED_HEAD=MOVED_HEAD, TMPDIR=self.tmp, **extra))
        self.assertEqual(expect, result.returncode, result.stdout + result.stderr)
        if result.returncode != -9:
            self.gate_stamp.write_text(f"started={started}\nended={utc(now())}\nexit={result.returncode}\n")
        return result

    def later_try(self, exit_code=None):
        """A job run that started after the record ended: with an exit, or none (killed)."""
        ended = self.record()["ended"]
        started = utc(datetime.datetime.strptime(ended, "%Y-%m-%dT%H:%M:%SZ") + datetime.timedelta(minutes=1))
        body = f"started={started}\n"
        if exit_code is not None:
            body += f"ended={started}\nexit={exit_code}\n"
        self.gate_stamp.write_text(body)

    def makes(self):
        return [line for line in self.calls("make ") if line.startswith("make ")]

    def record(self):
        return dict(line.split("=", 1) for line in self.gate_record.read_text().splitlines())

    def gate_line(self, **extra):
        out = self.run_promote(**extra).stdout
        lines = [line for line in out.splitlines() if "weekly gate" in line]
        self.assertEqual(1, len(lines), out)
        return lines[0]


class DoctorGate(GateCase):
    """`doctor gate` writes the newest decisive verdict, and nothing else."""

    def test_a_pass_records_both_heads_after_two_gated_checks(self):
        out = self.run_gate().stdout
        self.assertIn(f"  system: make check passed in {ROOT} at {SYSTEM_HEAD}", out)
        record = self.record()
        self.assertEqual(("pass", SYSTEM_HEAD, "0", PACK_HEAD, "0"),
                         (record["verdict"], record["system"], record["system_exit"],
                          record["pack"], record["pack_exit"]))
        self.assertRegex(record["ended"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        gated = self.calls("sd gate run")
        self.assertEqual(2, len(gated), gated)
        for line, folder in zip(gated, (ROOT, self.fixture.pack)):
            self.assertIn("gate run --max-wait 3600 --label", line)
            self.assertTrue(line.endswith(f"sh {folder} " + line.rsplit(" ", 1)[1]), line)
        self.assertEqual([f"make -C {ROOT} check", f"make -C {self.fixture.pack} check"], self.makes())
        self.assertFalse(self.gate_record.with_name("doctor-gate-last.new").exists())

    def test_a_failed_check_records_a_failure_and_exits_1(self):
        out = self.run_gate(expect=1, STUB_MAKE_pack="2").stdout
        self.assertIn(f"  pack: make check FAILED in {self.fixture.pack} at {PACK_HEAD} (exit 2)", out)
        self.assertEqual(("fail", "0", "2"), (self.record()["verdict"], self.record()["system_exit"],
                                              self.record()["pack_exit"]))

    def test_a_slot_timeout_writes_no_record_and_exits_75(self):
        out = self.run_gate(expect=75, STUB_GATE_system="75").stdout
        self.assertIn("no gate slot came free in 3600 s (exit 75); no verdict recorded", out)
        self.assertFalse(self.gate_record.exists())
        self.assertEqual([], self.makes(), "a check ran after a timeout")

    def test_a_check_that_did_not_start_or_was_signalled_writes_no_record(self):
        for code in ("125", "127", "143", "137"):
            with self.subTest(exit=code):
                self.run_gate(expect=1, STUB_GATE_pack=code)
                self.assertFalse(self.gate_record.exists())

    def test_a_checkout_moved_during_its_check_writes_no_record(self):
        """repo-sync's refresh moved the checkout under the check: it ran a mix."""
        out = self.run_gate(expect=1, STUB_MOVE="system", STUB_MAKE_system="2").stdout
        self.assertIn(f"moved from {SYSTEM_HEAD} to {MOVED_HEAD} during make check; no verdict recorded", out)
        self.assertFalse(self.gate_record.exists())

    def test_an_unread_head_writes_no_record(self):
        (self.heads / "pack").unlink()
        self.run_gate(expect=1)
        self.assertFalse(self.gate_record.exists())

    def test_a_system_failure_is_recorded_whatever_the_pack_check_does(self):
        """Review round 1: a later stage with no verdict never discards a failure before it."""
        later = {
            "a slot timeout": {"STUB_GATE_pack": "75"},
            "a check that did not start (125)": {"STUB_GATE_pack": "125"},
            "a check that did not start (127)": {"STUB_GATE_pack": "127"},
            "a signalled check (143)": {"STUB_GATE_pack": "143"},
            "a signalled check (137)": {"STUB_GATE_pack": "137"},
            "a moved checkout": {"STUB_MOVE": "pack"},
            "an unread head": {"unread": "pack"},
        }
        for name, extra in later.items():
            with self.subTest(name):
                # An older pass, which the failure must replace.
                self.write_gate_record("pass", datetime.timedelta(hours=2))
                (self.heads / "pack").write_text(PACK_HEAD + "\n")
                if extra.pop("unread", None):
                    (self.heads / "pack").unlink()
                out = self.run_gate(expect=1, STUB_MAKE_system="2", **extra).stdout
                self.assertIn(f"  system: make check FAILED in {ROOT} at {SYSTEM_HEAD} (exit 2)", out)
                self.assertIn("no pack verdict; the system failure is recorded", out)
                record = self.record()
                self.assertEqual(("fail", SYSTEM_HEAD, "2"),
                                 (record["verdict"], record["system"], record["system_exit"]))
                self.assertNotIn("pack", record)
                line = self.gate_line()
                self.assertTrue(line.startswith(f"  NOT-READY weekly gate FAILED, ended {record['ended']} "
                                                f"(system {SYSTEM_HEAD[:12]} exit 2; pack no verdict)"), line)
                self.run_promote("--apply", expect=1)
                self.assertEqual([], self.calls("restore"))
        with self.subTest("the run killed during the pack check"):
            self.write_gate_record("pass", datetime.timedelta(hours=2))
            self.run_gate(expect=-9, STUB_MAKE_system="2", STUB_GATE_pack="kill")
            record = self.record()
            self.assertEqual(("fail", "2"), (record["verdict"], record["system_exit"]))
            self.assertNotIn("pack", record)
            self.assertIn("NOT-READY weekly gate FAILED", self.gate_line())


class GateLine(GateCase):
    """hub-ready's gate line: the record decides; the job's stamp adds a note."""

    def test_pass_fail_timeout_reads_not_ready(self):
        """A pass, then a failure, then a slot timeout: the failure is the record."""
        self.run_gate()
        self.run_gate(expect=1, STUB_MAKE_system="2")
        failed = self.gate_record.read_text()
        self.run_gate(expect=75, STUB_GATE_system="75")
        self.assertEqual(failed, self.gate_record.read_text())
        self.later_try(75)
        line = self.gate_line()
        self.assertTrue(line.startswith("  NOT-READY weekly gate FAILED, ended "), line)
        self.assertTrue(line.endswith("; last try timed out"), line)
        result = self.run_promote("--apply", expect=1)
        self.assertIn("a hub-ready line is not ok; nothing moved", result.stderr)
        self.assertTrue(self.hub_json.exists())

    def test_a_failed_weekly_gate_is_not_ready(self):
        self.run_gate(expect=1, STUB_MAKE_system="2")
        line = self.gate_line()
        self.assertIn(f"  NOT-READY weekly gate FAILED, ended {self.record()['ended']} "
                      f"(system {SYSTEM_HEAD[:12]} exit 2; pack {PACK_HEAD[:12]} exit 0)", line)
        self.assertNotIn("last try", line)
        self.run_promote("--apply", expect=1)
        self.assertEqual([], self.calls("restore"))
        # A later pass is the record, and the line reads ok.
        self.run_gate()
        self.assertTrue(self.gate_line().startswith("  ok      weekly gate passed, ended "))

    def test_a_timeout_after_a_failure_stays_not_ready(self):
        self.run_gate(expect=1, STUB_MAKE_pack="1")
        self.later_try(75)
        line = self.gate_line()
        self.assertIn("NOT-READY weekly gate FAILED", line)
        self.assertTrue(line.endswith("; last try timed out"), line)

    def test_a_timeout_after_a_pass_stays_ok_with_a_note(self):
        self.run_gate()
        self.later_try(75)
        line = self.gate_line()
        self.assertTrue(line.startswith("  ok      weekly gate passed"), line)
        self.assertTrue(line.endswith("; last try timed out"), line)

    def test_a_killed_gate_run_keeps_the_last_verdict(self):
        self.run_gate()
        passed = self.gate_record.read_text()
        result = self.run_gate(expect=-9, STUB_GATE_pack="kill")
        self.assertEqual(passed, self.gate_record.read_text(), result.stdout)
        # The stamp holds a start and no end, as a run killed unseen leaves it.
        self.later_try()
        line = self.gate_line()
        self.assertTrue(line.startswith("  ok      weekly gate passed"), line)
        self.assertTrue(line.endswith("; last try did not finish"), line)
        # A run that ended with no verdict, as a moved checkout's, reads the same.
        self.later_try(1)
        self.assertTrue(self.gate_line().endswith("; last try did not finish"))

    def test_a_record_cut_short_keeps_the_old_one(self):
        """Killed between the temp file and the rename: the old record stands whole."""
        self.run_gate()
        passed = self.gate_record.read_text()
        write_stub(self.fixture.stubs, "mv", MV_STUB)
        self.run_gate(expect=-9, STUB_MAKE_system="2", STUB_MV_KILL="1")
        self.assertEqual(passed, self.gate_record.read_text())
        self.assertIn("verdict=fail", self.gate_record.with_name("doctor-gate-last.new").read_text())
        self.assertTrue(self.gate_line().startswith("  ok      weekly gate passed"))
        # The next run writes over the temp file and renames it.
        self.run_gate(expect=1, STUB_MAKE_system="2")
        self.assertEqual("fail", self.record()["verdict"])
        self.assertFalse(self.gate_record.with_name("doctor-gate-last.new").exists())

    def test_no_pass_in_8_days_is_not_ready(self):
        record = self.gate_record
        cases = {
            "no record": None,
            "a pass 8 days and a minute ago": datetime.timedelta(days=8, minutes=1),
        }
        for name, ago in cases.items():
            with self.subTest(name):
                if ago is None:
                    record.unlink(missing_ok=True)
                    self.assertTrue(self.gate_line().startswith(f"  NOT-READY no weekly gate verdict in {record}: "
                                                                f"run machine-setup.sh doctor gate"))
                else:
                    ended = self.write_gate_record("pass", ago)
                    self.assertTrue(self.gate_line().startswith(f"  NOT-READY weekly gate last passed {ended}, "
                                                                f"8 days ago or more"))
        ended = self.write_gate_record("pass", datetime.timedelta(days=8) - datetime.timedelta(minutes=1))
        self.assertTrue(self.gate_line().startswith(f"  ok      weekly gate passed, ended {ended} "))
        self.run_promote("--apply")

    def test_a_record_that_cannot_be_read_is_not_ready(self):
        """Extreme and broken values: each is no verdict, never a pass."""
        self.write_gate_record("pass", datetime.timedelta(hours=1))
        good = self.gate_record.read_text()
        bodies = {
            "empty": "",
            "binary": "\x00\xff\xfe",
            "no end": good.replace("ended=", "began="),
            "an end with no zone": re.sub(r"ended=(\S+)Z", r"ended=\1", good),
            "year zero": re.sub(r"ended=\S+", "ended=0000-01-01T00:00:00Z", good),
            "month 13": re.sub(r"ended=\d{4}-\d\d", "ended=2026-13", good),
            "a short head": good.replace("c" * 40, "c" * 39),
            "an upper-case head": good.replace("c" * 40, "C" * 40),
            "no exit": good.replace("pack_exit=0\n", ""),
            "a negative exit": good.replace("pack_exit=0", "pack_exit=-1"),
            "another verdict": good.replace("verdict=pass", "verdict=ok"),
            "a pass with no pack stage": re.sub(r"pack=\S+\npack_exit=0\n", "", good),
            "a pass naming an exit": good.replace("pack_exit=0", "pack_exit=2"),
            "a failure whose stages passed": good.replace("verdict=pass", "verdict=fail"),
            "a failure naming only the pack": re.sub(r"system=\S+\nsystem_exit=0\n", "",
                                                     good.replace("verdict=pass", "verdict=fail")
                                                     .replace("pack_exit=0", "pack_exit=2")),
        }
        for name, body in bodies.items():
            with self.subTest(name):
                self.gate_record.write_bytes(body.encode("latin-1"))
                line = self.gate_line()
                self.assertTrue(line.startswith(f"  NOT-READY weekly gate record {self.gate_record} cannot be read ("),
                                line)
        with self.subTest("ends far in the future"):
            self.gate_record.write_text(re.sub(r"ended=\S+", "ended=9999-12-31T23:59:59Z", good))
            self.assertIn("in the future", self.gate_line())
            self.assertIn("NOT-READY", self.gate_line())
        with self.subTest("a huge exit is still a failure"):
            self.gate_record.write_text(good.replace("verdict=pass", "verdict=fail")
                                        .replace("pack_exit=0", "pack_exit=" + "9" * 40))
            self.assertIn("NOT-READY weekly gate FAILED", self.gate_line())
        with self.subTest("unreadable"):
            self.gate_record.write_text(good)
            self.gate_record.chmod(0)
            self.addCleanup(self.gate_record.chmod, 0o600)
            self.assertIn("cannot be read", self.gate_line())

    def test_a_stamp_from_before_the_record_adds_no_note(self):
        self.run_gate()
        self.gate_stamp.write_text("started=2000-01-01T00:00:00Z\nended=2000-01-01T00:00:01Z\nexit=75\n")
        self.assertNotIn("last try", self.gate_line())
        self.gate_stamp.write_text("garbage\n")
        self.assertNotIn("last try", self.gate_line())


class BackupAge(PromoteCase):
    """hub-ready's backup age line, from the snapshots' own checkpoints."""

    def setUp(self):
        super().setUp()
        self.source_home = self.tmp / "hub-home"
        self.source = self.source_home / ".local/share/sd/sd.db"
        self.source.parent.mkdir(parents=True)
        initialise(self.source)
        self.hourly_nas = self.nas / "sd-backups-hourly"

    def snapshot(self, ago, root=None):
        """A snapshot `sd-db backup` took `ago` before now."""
        taken = take_backup(home=self.source_home, database=self.source, destination=root or self.hourly_nas,
                           when=now() - ago)
        return taken.directory

    def age_line(self, **extra):
        out = self.run_promote(**extra).stdout
        lines = [line for line in out.splitlines() if "backup age" in line]
        self.assertEqual(1, len(lines), out)
        return lines[0]

    def answering_hub(self):
        hub = socket.socket()
        hub.bind(("127.0.0.1", 0))
        hub.listen()
        self.addCleanup(hub.close)
        self.hub_json.write_text(f'{{"hub": "127.0.0.1", "port": {hub.getsockname()[1]}}}\n')
        return {"STUB_HUB_NAME": "127.0.0.1"}

    def test_a_stalled_backup_is_not_ready_while_the_hub_answers(self):
        old = self.snapshot(datetime.timedelta(hours=30))
        line = self.age_line(**self.answering_hub())
        self.assertTrue(line.startswith("  NOT-READY backup age 30 h, 24 h or more: newest checkpoint "), line)
        self.assertTrue(line.endswith(f" in {old}; the hub answers, so its backups have not reached the roots in 24 h"),
                        line)
        # The next hourly copy lands: ok.
        fresh = self.snapshot(datetime.timedelta(hours=2))
        line = self.age_line(**self.answering_hub())
        self.assertTrue(line.startswith("  ok      backup age 2 h: newest checkpoint "), line)
        self.assertTrue(line.endswith(f" in {fresh}"), line)

    def test_the_24_hour_edge(self):
        hub = self.answering_hub()
        under = self.snapshot(datetime.timedelta(hours=24) - datetime.timedelta(minutes=2))
        self.assertTrue(self.age_line(**hub).startswith("  ok      backup age 23 h"))
        shutil.rmtree(under)
        self.snapshot(datetime.timedelta(hours=24, minutes=1))
        self.assertTrue(self.age_line(**hub).startswith("  NOT-READY backup age 24 h, 24 h or more"))

    def test_no_checkpoint_at_all_is_not_ready_while_the_hub_answers(self):
        """The fixture's snapshots carry no readable manifest: no age, never ok."""
        line = self.age_line(**self.answering_hub())
        self.assertEqual("  NOT-READY backup age unknown: no snapshot under the roots that answered passes "
                         "restore's checks; "
                         "the hub answers, so its backups have not reached the roots in 24 h", line)

    def test_a_checkpoint_in_the_future_is_no_age(self):
        self.snapshot(-datetime.timedelta(days=3650))
        line = self.age_line(**self.answering_hub())
        self.assertTrue(line.startswith("  NOT-READY backup age unknown: newest checkpoint "), line)
        self.assertTrue(line.endswith(" lies in the future; the hub answers, so a clock is wrong"), line)

    def test_an_old_snapshot_with_the_hub_down_does_not_block(self):
        old = self.snapshot(datetime.timedelta(hours=50))
        line = self.age_line()
        self.assertTrue(line.startswith("  --      backup age 50 h, 24 h or more: newest checkpoint "), line)
        self.assertIn(f" in {old}; the hub does not answer, so promote restores the newest there is", line)
        result = self.run_promote("--apply")
        self.assertIn("  checkpoint  : ", result.stdout)
        self.assertIsNone(self.marker_step())

    def test_a_partial_snapshot_is_not_the_newest(self):
        """A copy cut short after its manifest lacks a file; the line reads the next one."""
        older = self.snapshot(datetime.timedelta(hours=30))
        for cut in ("sd.db", "backup-manifest.json"):
            with self.subTest(cut=cut):
                partial = self.snapshot(datetime.timedelta(hours=1))
                (partial / cut).unlink()
                line = self.age_line()
                self.assertTrue(line.startswith("  --      backup age 30 h"), line)
                self.assertIn(f" in {older};", line)
                shutil.rmtree(partial)
        with self.subTest("a folder it lists is gone"):
            partial = self.snapshot(datetime.timedelta(hours=1))
            manifest = partial / "backup-manifest.json"
            value = json.loads(manifest.read_text())
            value["entries"]["publications"] = None
            manifest.write_text(json.dumps(value))
            self.assertIn(f" in {older};", self.age_line())
            (partial / "publications").mkdir()
            self.assertIn(f" in {partial}", self.age_line())

    def test_a_snapshot_restore_would_refuse_is_not_the_newest(self):
        """Review round 2: the line judges a snapshot as restore --newest does, hashes and all."""
        hub = self.answering_hub()
        older = self.snapshot(datetime.timedelta(hours=30))

        def flip_a_byte(folder):
            data = bytearray((folder / "sd.db").read_bytes())
            data[-1] ^= 0xFF
            (folder / "sd.db").write_bytes(bytes(data))

        def cut_short(folder):
            data = (folder / "sd.db").read_bytes()
            (folder / "sd.db").write_bytes(data[:len(data) // 2])

        def add_a_file(folder):
            (folder / "stray.txt").write_text("written after the manifest\n")

        for damage in (flip_a_byte, cut_short, add_a_file):
            with self.subTest(damage.__name__):
                bad = self.snapshot(datetime.timedelta(hours=1))
                damage(bad)
                line = self.age_line(**hub)
                self.assertTrue(line.startswith("  NOT-READY backup age 30 h, 24 h or more: "), line)
                self.assertIn(f" in {older};", line)
                shutil.rmtree(bad)
        with self.subTest("no snapshot passes"):
            flip_a_byte(older)
            self.assertEqual("  NOT-READY backup age unknown: no snapshot under the roots that answered passes "
                             "restore's checks; the hub answers, so its backups have not reached the roots in 24 h",
                             self.age_line(**hub))

    def test_a_newer_schema_is_not_ready_even_with_the_hub_down(self):
        """restore --newest stops at it rather than restore an older one, so promote would fail."""
        self.snapshot(datetime.timedelta(hours=30))
        newer = self.snapshot(datetime.timedelta(hours=1))
        data = bytearray((newer / "sd.db").read_bytes())
        data[60:64] = (999999).to_bytes(4, "big")  # PRAGMA user_version, the schema version
        (newer / "sd.db").write_bytes(bytes(data))
        manifest = newer / "backup-manifest.json"
        value = json.loads(manifest.read_text())
        value["entries"]["sd.db"] = hashlib.sha256(bytes(data)).hexdigest()
        manifest.write_text(json.dumps(value))
        line = self.age_line()
        self.assertTrue(line.startswith(f"  NOT-READY backup age unknown: {newer} is schema 999999, newer than "
                                        "this pack ("), line)
        self.assertTrue(line.endswith("); restore --newest stops there: update the pack"), line)

    def test_the_newest_checkpoint_wins_across_roots_whatever_the_names(self):
        """restore --newest orders by checkpoint, not by root or name."""
        nightly = self.nas / "sd-backups"
        self.snapshot(datetime.timedelta(hours=40))
        newest = self.snapshot(datetime.timedelta(hours=3), root=nightly)
        renamed = newest.with_name("2000-01-01")
        newest.rename(renamed)
        self.assertTrue(self.age_line().startswith("  ok      backup age 3 h"))
        self.assertTrue(self.age_line().endswith(f" in {renamed}"))

    def test_an_unlisted_root_gives_no_age(self):
        """A root whose listing fails or stalls is not read for an age, and no age reads ok."""
        self.snapshot(datetime.timedelta(hours=1))
        self.hourly_nas.chmod(0)
        self.addCleanup(self.hourly_nas.chmod, 0o755)
        out = self.run_promote().stdout
        self.assertIn(f"  --      backup root not readable now: {self.hourly_nas}", out)
        self.assertNotIn("ok      backup age", out)
        self.assertIn("  --      backup age unknown: no snapshot under the roots that answered passes "
                      "restore's checks;", out)

    def test_a_reader_that_times_out_gives_no_age(self):
        self.snapshot(datetime.timedelta(hours=1))
        hang = write_stub(self.fixture.stubs, "hang-python",
                          'case "$5" in backup-age) exec sleep 30 ;; esac\n' + f'exec "{self.prove}" "$@"\n')
        env = self.fixture.env(**self.promote_env(SD_DB_PYTHON=hang, MACHINE_SETUP_STEP_TIMEOUT="2",
                                                  ST_BOUNDED_GRACE="1"))
        result = subprocess.run([str(SCRIPT), "promote"], env=env, capture_output=True, text=True,
                                cwd=str(self.tmp), stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("  --      backup age not read: the snapshots were not checked under", result.stdout)
        self.assertNotIn("ok      backup age", result.stdout)
        self.assertNotIn("NOT-READY", result.stdout)

    def stalled_copy(self, then="pass"):
        """Promote with a reader that copies a snapshot, runs `then`, and hangs.

        Returns the result, the run's own TMPDIR and the path of the copy: a
        stop at the bound or a TERM leaves neither copy nor folder (sd:6325).
        """
        self.snapshot(datetime.timedelta(hours=1))
        copied = self.tmp / "copied"
        temp = self.tmp / "run-tmp"
        temp.mkdir()
        stall = write_stub(self.fixture.stubs, "stall-python", r'''case "$5" in backup-age)
  code=$3; shift 3
  exec "$STUB_REAL_PYTHON" -I -c "import os, shutil, signal, time
_copy = shutil.copytree
def copytree(source, local, **kwargs):
    _copy(source, local, **kwargs)
    open(os.environ['STUB_COPIED'], 'w').write(str(local))
    $STUB_THEN
    time.sleep(30)
shutil.copytree = copytree
$code" "$@" ;;
esac
''' + f'exec "{self.prove}" "$@"\n')
        env = self.fixture.env(**self.promote_env(SD_DB_PYTHON=stall, MACHINE_SETUP_STEP_TIMEOUT="4",
                                                  ST_BOUNDED_GRACE="1", TMPDIR=temp, STUB_COPIED=copied,
                                                  STUB_THEN=then))
        result = subprocess.run([str(SCRIPT), "promote"], env=env, capture_output=True, text=True,
                                cwd=str(self.tmp), stdin=subprocess.DEVNULL, timeout=120, start_new_session=True)
        self.assertTrue(copied.exists(), result.stdout + result.stderr)
        return result, temp, Path(copied.read_text())

    def assert_no_staging(self, temp, local):
        self.assertFalse(local.exists(), local)
        self.assertFalse(local.parent.exists(), local.parent)
        self.assertEqual([], sorted(temp.glob("hub-ready-*")))

    def test_a_reader_stopped_at_its_bound_leaves_no_copy(self):
        result, temp, local = self.stalled_copy()
        self.assertIn("  --      backup age not read: the snapshots were not checked under", result.stdout)
        self.assert_no_staging(temp, local)

    def test_a_run_terminated_during_the_copy_leaves_no_copy(self):
        """cron-jobs TERMs a job's group at its limit; the reader dies in the copy."""
        result, temp, local = self.stalled_copy("os.killpg(os.getpgid(os.getppid()), signal.SIGTERM)")
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assert_no_staging(temp, local)

    def test_no_interpreter_gives_no_age(self):
        self.snapshot(datetime.timedelta(hours=1))
        out = self.run_promote(SD_DB_PYTHON=os.devnull).stdout
        self.assertIn("  --      backup age not read:", out)

    def age_line_from(self, backup_py, **extra):
        """The line with `backup_py` as the pack's sd_db/backup.py, or no sd_db when None."""
        lib = self.tmp / "other-lib"
        (lib / "sd_db").mkdir(parents=True)
        if backup_py is None:
            (lib / "sd_db").rmdir()
        else:
            (lib / "sd_db/__init__.py").write_text("")
            (lib / "sd_db/backup.py").write_text(backup_py)
        swap = write_stub(self.fixture.stubs, "swap-python", '''case "$5" in backup-age)
  a=$1 b=$2 c=$3; shift 4
  exec "$STUB_REAL_PYTHON" "$a" "$b" "import os, sys
sys.path.extend(filter(None, [os.environ.get('STUB_ELSEWHERE')]))
$c" "$STUB_LIB" "$@" ;;
esac
''' + f'exec "{self.prove}" "$@"\n')
        return self.age_line(SD_DB_PYTHON=swap, STUB_LIB=lib, **extra)

    def test_the_line_reads_the_packs_walk(self):
        """sd:6331: the pack's newest_restorable picks the snapshot; no copy of its walk here."""
        called = self.tmp / "called.json"
        line = self.age_line_from('''import datetime, json, os, tempfile
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class Restorable:
    snapshot: object
    taken_at: object
    refused: list
    released: list
    newer: object = None


def newest_restorable(roots, *, report=None):
    roots = [str(root) for root in roots]
    Path(os.environ["STUB_CALLED"]).write_text(json.dumps({"roots": roots, "tmp": tempfile.gettempdir()}))
    taken = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5, minutes=1)
    return Restorable(Path(roots[0]) / "2026-10-26.7", taken, [], [])
''', STUB_CALLED=called, **self.answering_hub())
        call = json.loads(called.read_text())
        self.assertEqual(sorted([str(self.hourly_nas), str(self.nas / "sd-backups")]), sorted(call["roots"]))
        self.assertTrue(line.startswith("  ok      backup age 5 h: newest checkpoint "), line)
        self.assertTrue(line.endswith(f" in {call['roots'][0]}/2026-10-26.7"), line)
        # Its copies go in the folder the shell removes (sd:6325).
        self.assertTrue(Path(call["tmp"]).name.startswith("hub-ready-"), call["tmp"])

    def test_an_older_pack_is_a_finding_not_a_traceback(self):
        """A pack from before pack sd:6323 has no newest_restorable."""
        self.snapshot(datetime.timedelta(hours=1))
        pack = self.fixture.pack
        self.assertEqual(f"  --      backup age not read: {pack}/lib has no sd_db.backup.newest_restorable "
                         "(pack sd:6323); update the pack", self.age_line_from("SCHEMA_VERSION = 1\n"))
        # The gate's interpreter has the pinned pack's sd_db installed: a copy
        # it finds elsewhere is not this pack's, so still no answer.
        for elsewhere in ("", str(PACK / "lib")):
            with self.subTest(elsewhere=elsewhere):
                shutil.rmtree(self.tmp / "other-lib", ignore_errors=True)
                line = self.age_line_from(None, STUB_ELSEWHERE=elsewhere)
                self.assertTrue(line.startswith("  --      backup age not read: "), line)
                self.assertTrue(line.endswith(f" imports no sd_db.backup from {pack}/lib"), line)
