"""Tests for refresh's backup, migrate and recovery (pack design D2, sd:3345).

When the database's schema is below the SCHEMA_VERSION of the pack's lib/
that refresh moves to, refresh backs the database up and writes one record,
the rollback point, before anything moves. Then it moves, migrates, and on a
failure puts the database and both checkouts back at the record. Every rerun
reuses the record. These cases run against a real database and the pack's
real `bin/sd-db` and `lib/sd_db`, copied from the pack the gate pins
(`SD_ACCEPTANCE_PACK`), with fixture migrations added after its last one.
`make` and `launchctl` are the stubs of `test_pinned.py`.

A kill is the make stub killing refresh during the pack's `make setup`, after
the move and before the migrate. A state no hook reaches, such as a kill
inside `migrate` or during the recovery, is built by running the same real
steps by hand. Every case is NEW: written before the change and seen to fail
against the script without it.
"""

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_follow import FollowFixture  # noqa: E402
from test_pinned import DASHBOARD, SERVE  # noqa: E402
from test_repo_sync import FOLDER, GIT_WRAPPER  # noqa: E402

ROOT = FOLDER.parent

# Copies the record, when there is one, so a case can read what refresh wrote
# after it deleted it. The make stub runs this at every make setup.
CAPTURE = '[ ! -e "$XDG_STATE_HOME/repo-sync/refresh-intent" ] || cp "$XDG_STATE_HOME/repo-sync/refresh-intent" "$RECORD_SEEN"'
# A kill of refresh after the pack moved, before the migrate.
KILL = CAPTURE + '; kill -KILL $PPID'

# Logs each bootstrap with the database's schema and whether the record is
# there, then runs LAUNCHCTL_HOOK and the stub of test_pinned.py.
LAUNCHCTL_SEEN = """#!/bin/sh
if [ "$1" = bootstrap ]; then
  record=no
  [ ! -e "$XDG_STATE_HOME/repo-sync/refresh-intent" ] || record=yes
  printf '%s schema=%s record=%s\\n' "$(basename "$3" .plist)" \\
    "$(sqlite3 -readonly "$REPO_SYNC_SD_DB" 'PRAGMA user_version' 2>/dev/null)" "$record" >> "$STARTS_LOG"
  eval "${LAUNCHCTL_HOOK:-}"
fi
exec "$(dirname "$0")/launchctl-stub" "$@"
"""


def source_pack():
    """The pack the gate pins, or an AssertionError that says how to get one."""
    pack = pathlib.Path(os.environ.get("SD_ACCEPTANCE_PACK") or ROOT / ".ci" / "pack")
    if not (pack / "bin" / "sd-db").is_file() or not (pack / "lib" / "sd_db" / "schema.py").is_file():
        raise AssertionError(f"no pack with bin/sd-db and lib/sd_db at {pack}: run `make check`, "
                             "which fetches the pack at the pin into .ci/pack, or set SD_ACCEPTANCE_PACK")
    return pack


def schema_of(text):
    return next(int(line.split("=")[1]) for line in text.splitlines() if line.startswith("SCHEMA_VERSION = "))


class MigrateFixture(FollowFixture):
    """A hub: a pinned system and a pinned pack, each behind its origin, a
    real database at the pack's schema N, and both services loaded."""

    def __init__(self):
        super().__init__()
        source = source_pack()
        (self.bin / "launchctl").rename(self.bin / "launchctl-stub")
        (self.bin / "launchctl").write_text(LAUNCHCTL_SEEN)
        (self.bin / "launchctl").chmod(0o755)
        # bin/sd-db runs `env python3`; this run's interpreter is new enough.
        (self.bin / "python3").symlink_to(sys.executable)
        self.home = self.tmp / "home"
        self.database = self.home / ".local/share/sd/sd.db"
        self.state = self.tmp / "state" / "repo-sync"
        self.record = self.state / "refresh-intent"
        self.backups = self.state / "refresh-backups"
        self.record_seen = self.tmp / "record.seen"
        self.starts_log = self.tmp / "starts.log"

        self.system = self.system_repo()
        self.pack = self.repo("a/pack")
        for name in ("sd-db", "sd_lib.py", "sd_library_guard.py"):
            (self.pack / "bin").mkdir(exist_ok=True)
            shutil.copy2(source / "bin" / name, self.pack / "bin" / name)
        (self.pack / "bin" / "sd_install.py").write_text("")
        shutil.copytree(source / "lib", self.pack / "lib",
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"))
        self.git(self.pack, "add", "-A")
        self.git(self.pack, "commit", "-q", "-m", "the pack's bin/sd-db and lib/")
        self.git(self.pack, "push", "-q", "origin", "main")
        self.n = schema_of((self.pack / "lib/sd_db/schema.py").read_text())
        for repo in (self.system, self.pack):
            self.pin(repo)
        self.a_system, self.a_pack = self.head(self.system), self.head(self.pack)
        self.b_system = self.advance_origin(self.system)
        # The pack's origin, written through one clone that gains each fixture migration.
        self.upstream = self.tmp / "upstream"
        self.git(self.tmp, "clone", "-q", "-b", "main", self.bare(self.pack), str(self.upstream))
        self.sd_db("init")
        for label in (SERVE, DASHBOARD):
            (self.launchd / label).touch()

    def run(self, *args, expect=0, extra_env=None):
        env = {"REPO_SYNC_SD_DB": str(self.database), "RECORD_SEEN": str(self.record_seen),
               "STARTS_LOG": str(self.starts_log), "MAKE_HOOK": CAPTURE,
               "SYSTEM_TOOLS_CONFIG": str(self.tmp / "config"), "PYTHONDONTWRITEBYTECODE": "1",
               **(extra_env or {})}
        return super().run(*args, expect=expect, extra_env=env)

    def sd_db(self, *args, check=True):
        """The pack checkout's bin/sd-db, as refresh runs it, on the fixture home."""
        env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        result = subprocess.run([str(self.pack / "bin" / "sd-db"), *args], env=env,
                                capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise AssertionError(f"sd-db {' '.join(args)} exited {result.returncode}: {result.stderr}")
        return result

    def upstream_migrations(self, *bodies, replace=False):
        """Push one commit to the pack's origin adding a migration per body,
        after the last one there, and bumping SCHEMA_VERSION. With `replace`,
        rewrite the last migration's body instead. Returns the new sha."""
        schema = self.upstream / "lib/sd_db/schema.py"
        text = schema.read_text()
        version = schema_of(text)
        folder = self.upstream / "lib/sd_db/schema"
        if replace:
            (path,) = folder.glob(f"{version:03d}_*.sql")
            path.write_text(bodies[0])
        else:
            for body in bodies:
                version += 1
                (folder / f"{version:03d}_fixture_{version}.sql").write_text(body)
            schema.write_text(text.replace(f"SCHEMA_VERSION = {schema_of(text)}\n", f"SCHEMA_VERSION = {version}\n"))
        self.git(self.upstream, "add", "-A")
        self.git(self.upstream, "commit", "-q", "-m", f"schema {version}")
        self.git(self.upstream, "push", "-q", "origin", "main")
        return self.head(self.upstream)

    @staticmethod
    def good(name):
        return f"CREATE TABLE fixture_{name} (a INTEGER);\n"

    @staticmethod
    def bad(name):
        return f"CREATE TABLE fixture_{name} (a INTEGER);\nINSERT INTO fixture_no_such_table VALUES (1);\n"

    def version(self):
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    def restores(self):
        connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return [row[0] for row in connection.execute("SELECT body FROM state WHERE kind = 'restore'")]
        finally:
            connection.close()

    def backup_dirs(self):
        if not self.backups.exists():
            return []
        return sorted(str(d) for root in self.backups.iterdir() for d in root.iterdir() if d.is_dir())

    def loaded(self):
        return sorted(p.name for p in self.launchd.iterdir() if p.suffix != ".pending")

    def starts(self):
        return self.starts_log.read_text().splitlines() if self.starts_log.exists() else []

    def stop_services(self):
        """What a refresh killed after its bootouts leaves: both listed, neither loaded."""
        for label in (SERVE, DASHBOARD):
            (self.launchd / label).unlink(missing_ok=True)
        stopped = self.state / "refresh-stopped"
        stopped.parent.mkdir(parents=True, exist_ok=True)
        stopped.write_text(f"{SERVE}\n{DASHBOARD}\n")

    def take_backup(self):
        """A backup the way refresh takes one; returns its directory."""
        root = self.backups / f"by-hand-{len(self.backup_dirs())}"
        self.sd_db("backup", "--destination", str(root), "--no-row-prune")
        (directory,) = [d for d in root.iterdir() if d.is_dir()]
        return directory

    def write_record(self, text):
        self.record.parent.mkdir(parents=True, exist_ok=True)
        self.record.write_text(text)

    def record_text(self, system, pack, schema, backup):
        return f"system={system}\npack={pack}\nschema={schema}\nbackup={backup}\n"

    def field(self, text, name):
        return next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{name}="))


class RefreshMigrateTest(unittest.TestCase):
    def setUp(self):
        self.f = MigrateFixture()
        self.addCleanup(self.f.destroy)

    # --- helpers that read one end state -----------------------------------

    def assert_at_first_pair(self, f, backups=1):
        """Recovered: schema N, both checkouts at A, no record, the services
        started at N with no record, no hub-pin, no new backup."""
        self.assertEqual(f.n, f.version())
        self.assertEqual((f.a_system, f.a_pack), (f.head(f.system), f.head(f.pack)))
        self.assertFalse(f.record.exists())
        self.assertEqual([DASHBOARD, SERVE], f.loaded())
        self.assertEqual([f"{SERVE} schema={f.n} record=no", f"{DASHBOARD} schema={f.n} record=no"], f.starts())
        self.assertIsNone(f.hub_pin(f.system))
        self.assertEqual(backups, len(f.backup_dirs()))
        # Old code opens the database: the pack at A answers status.
        self.assertEqual(0, f.sd_db("status", check=False).returncode)

    def assert_stuck(self, f, result, record):
        """Unknown or failed: nothing started, the record unchanged, no hub-pin."""
        self.assertEqual([], f.loaded())
        self.assertEqual([], f.starts())
        self.assertEqual(record, f.record.read_text())
        self.assertIsNone(f.hub_pin(f.system))
        self.assertIn("step(s) failed", result.stdout)

    def killed_after_the_move(self, f):
        """Run a refresh that the make stub kills during the pack's make setup."""
        f.run("refresh", expect=None, extra_env={"MAKE_HOOK": KILL})
        self.assertTrue(f.record.exists())
        return f.record.read_text()

    # --- the flow ------------------------------------------------------------

    def test_a_schema_bump_backs_up_migrates_and_starts(self):
        f = self.f
        target = f.upstream_migrations(f.good("one"), f.good("two"))
        result = f.run("refresh", expect=0)
        self.assertEqual(f.n + 2, f.version())
        self.assertEqual(target, f.head(f.pack))
        self.assertFalse(f.record.exists())
        (backup,) = f.backup_dirs()
        self.assertEqual(f.record_text(f.a_system, f.a_pack, f.n, backup), f.record_seen.read_text())
        self.assertEqual([f"{SERVE} schema={f.n + 2} record=no", f"{DASHBOARD} schema={f.n + 2} record=no"],
                         f.starts())
        self.assertEqual((f.head(f.system), target), f.hub_pin(f.system))
        self.assertNotIn("migrates nothing", result.stdout)

    def test_an_unreadable_database_moves_nothing(self):
        f = self.f
        f.upstream_migrations(f.good("one"))
        f.database.write_bytes(b"not a database")
        for suffix in ("-wal", "-shm"):
            pathlib.Path(f"{f.database}{suffix}").unlink(missing_ok=True)
        result = f.run("refresh", expect=1)
        self.assertIn(f"cannot read the schema version of {f.database}", result.stdout)
        self.assertEqual((f.a_system, f.a_pack), (f.head(f.system), f.head(f.pack)))
        self.assertEqual([], f.backup_dirs())
        self.assertFalse(f.record.exists())
        self.assertEqual([], f.loaded())

    def test_a_failed_backup_moves_nothing_and_starts_the_services(self):
        f = self.f
        f.upstream_migrations(f.good("one"))
        f.state.mkdir(parents=True)
        f.backups.write_text("a file where the backups go\n")
        result = f.run("refresh", expect=1)
        self.assertIn("the backup before the migrate failed; nothing moved, no record written", result.stdout)
        self.assertEqual((f.a_system, f.a_pack), (f.head(f.system), f.head(f.pack)))
        self.assertFalse(f.record.exists())
        self.assertEqual(f.n, f.version())
        self.assertEqual([DASHBOARD, SERVE], f.loaded())
        self.assertNotIn(f"-C {f.pack} setup", f.make_calls())
        self.assertIsNone(f.hub_pin(f.system))

    def test_a_failed_record_write_moves_nothing_and_keeps_the_backup(self):
        f = self.f
        f.upstream_migrations(f.good("one"))
        (f.state / "refresh-intent.tmp").mkdir(parents=True)
        result = f.run("refresh", expect=1)
        self.assertIn(f"cannot write {f.record}; nothing moved", result.stdout)
        self.assertEqual((f.a_system, f.a_pack), (f.head(f.system), f.head(f.pack)))
        self.assertFalse(f.record.exists())
        self.assertEqual(1, len(f.backup_dirs()))
        self.assertEqual([DASHBOARD, SERVE], f.loaded())
        self.assertIsNone(f.hub_pin(f.system))

    def test_a_kill_before_the_record_takes_a_second_backup_on_rerun(self):
        f = self.f
        target = f.upstream_migrations(f.good("one"))
        # The state a kill between the backup and the record leaves: a
        # backup, no record, nothing moved, the services stopped.
        (f.state / "refresh-intent.tmp").mkdir(parents=True)
        f.run("refresh", expect=1)
        (f.state / "refresh-intent.tmp").rmdir()
        (first,) = f.backup_dirs()
        f.stop_services()
        f.run("refresh", expect=0)
        backups = f.backup_dirs()
        self.assertEqual(2, len(backups))
        self.assertIn(first, backups)
        second = next(b for b in backups if b != first)
        self.assertEqual(f.record_text(f.a_system, f.a_pack, f.n, second), f.record_seen.read_text())
        self.assertEqual(f.n + 1, f.version())
        self.assertEqual(target, f.head(f.pack))

    def test_a_record_left_with_nothing_moved_is_reused(self):
        f = self.f
        target = f.upstream_migrations(f.good("one"))
        record = f.record_text(f.a_system, f.a_pack, f.n, f.take_backup())
        f.write_record(record)
        f.stop_services()
        result = f.run("refresh", expect=0)
        self.assertIn("reused, no new backup", result.stdout)
        self.assertEqual(record, f.record_seen.read_text())
        self.assertEqual(1, len(f.backup_dirs()))
        self.assertEqual(f.n + 1, f.version())
        self.assertEqual(target, f.head(f.pack))
        self.assertFalse(f.record.exists())
        self.assertEqual([DASHBOARD, SERVE], f.loaded())

    def test_a_kill_after_the_move_reuses_the_record_and_migrates_before_a_start(self):
        f = self.f
        target = f.upstream_migrations(f.good("one"), f.good("two"))
        record = self.killed_after_the_move(f)
        self.assertEqual(target, f.head(f.pack))
        self.assertEqual(f.n, f.version())
        self.assertEqual([], f.loaded())
        f.record_seen.unlink()
        f.run("refresh", expect=0)
        self.assertEqual(record, f.record_seen.read_text())
        self.assertEqual(1, len(f.backup_dirs()))
        self.assertEqual(f.n + 2, f.version())
        self.assertEqual([f"{SERVE} schema={f.n + 2} record=no", f"{DASHBOARD} schema={f.n + 2} record=no"],
                         f.starts())
        self.assertEqual((f.head(f.system), target), f.hub_pin(f.system))

    # --- an unknown rollback point -----------------------------------------

    def refused_on_record(self, record):
        f = self.f
        f.upstream_migrations(f.good("one"))
        f.write_record(record)
        result = f.run("refresh", expect=1)
        self.assertIn(f"{f.record} is left by a migrate that did not finish, and its rollback point is unknown",
                      result.stdout)
        self.assertEqual((f.a_system, f.a_pack), (f.head(f.system), f.head(f.pack)))
        self.assertEqual(f.n, f.version())
        self.assert_stuck(f, result, record)
        return result

    def test_an_unreadable_record_refuses_and_keeps_services_stopped(self):
        self.refused_on_record("not a record\n")

    def test_a_missing_backup_refuses_and_keeps_services_stopped(self):
        f = self.f
        backup = f.take_backup()
        shutil.rmtree(backup)
        result = self.refused_on_record(f.record_text(f.a_system, f.a_pack, f.n, backup))
        self.assertIn(f"its backup {backup}/sd.db is missing or unreadable", result.stdout)

    def test_a_backup_at_another_version_refuses_and_keeps_services_stopped(self):
        f = self.f
        backup = f.take_backup()
        result = self.refused_on_record(f.record_text(f.a_system, f.a_pack, f.n + 5, backup))
        self.assertIn(f"reads schema {f.n}, not the record's {f.n + 5}", result.stdout)

    # --- a failed migrate ----------------------------------------------------

    def test_a_first_migration_that_fails_moves_both_back_without_a_restore(self):
        f = self.f
        f.upstream_migrations(f.bad("one"))
        result = f.run("refresh", expect=1)
        self.assertIn("bin/sd-db migrate", result.stdout)
        self.assert_at_first_pair(f)
        self.assertEqual([], f.restores())
        setups = [c for c in f.make_calls() if c == f"-C {f.pack} setup"]
        self.assertEqual(2, len(setups))

    def test_a_later_migration_that_fails_restores_the_backup(self):
        f = self.f
        f.upstream_migrations(f.good("one"), f.bad("two"))
        result = f.run("refresh", expect=1)
        self.assertIn(f"schema {f.n + 1} back to {f.n}", result.stdout)
        self.assert_at_first_pair(f)
        (backup,) = f.backup_dirs()
        (restore,) = f.restores()
        self.assertIn(backup, restore)

    def test_a_failed_restore_keeps_services_stopped_and_the_record(self):
        f = self.f
        f.upstream_migrations(f.good("one"), f.bad("two"))
        lock = f.home / ".local/share/sd/operation-locks/control.lock"
        lock.parent.mkdir(mode=0o700, exist_ok=True)
        holder = f.hold(lock, 600)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        result = f.run("refresh", expect=1)
        self.assertIn("!!! failed: " + str(f.pack) + "/bin/sd-db migrate", result.stdout)
        self.assertIn("!!! failed: the restore of", result.stdout)
        self.assertEqual(f.n + 1, f.version())
        self.assertEqual(f.a_pack, f.head(f.pack))
        self.assert_stuck(f, result, f.record_seen.read_text())

    def test_a_failed_move_back_keeps_services_stopped_and_the_record(self):
        f = self.f
        f.upstream_migrations(f.good("one"), f.bad("two"))
        # A tracked change the target holds makes the switch back refuse.
        dirty = ('grep -q fixture-dirty "$2/lib/sd_db/schema.py" '
                 '|| echo "# fixture-dirty" >> "$2/lib/sd_db/schema.py"; ' + CAPTURE)
        result = f.run("refresh", expect=1, extra_env={"MAKE_HOOK": dirty})
        self.assertIn("!!! failed: " + str(f.pack) + "/bin/sd-db migrate", result.stdout)
        self.assertIn(f"!!! failed: cannot put {f.pack} back at {f.a_pack}", result.stdout)
        self.assertEqual(f.n + 1, f.version())
        self.assert_stuck(f, result, f.record_seen.read_text())

    # --- reruns --------------------------------------------------------------

    def test_a_kill_after_one_migration_then_a_failing_next_recovers_to_the_first_pair(self):
        """Review round 6 of the design, with a target that moved on (B to C)."""
        f = self.f
        b = f.upstream_migrations(f.good("one"))
        self.killed_after_the_move(f)
        self.assertEqual(b, f.head(f.pack))
        f.sd_db("migrate")  # what a kill after N+1 committed leaves
        self.assertEqual(f.n + 1, f.version())
        f.upstream_migrations(f.bad("two"))
        f.run("refresh", expect=1)
        self.assert_at_first_pair(f)
        self.assertEqual(1, len(f.restores()))

    def test_a_kill_after_the_first_of_two_migrations_finishes_both_before_a_start(self):
        f = self.f
        f.upstream_migrations(f.good("one"))
        self.killed_after_the_move(f)
        f.sd_db("migrate")
        target = f.upstream_migrations(f.good("two"))
        f.run("refresh", expect=0)
        self.assertEqual(f.n + 2, f.version())
        self.assertEqual(1, len(f.backup_dirs()))
        self.assertFalse(f.record.exists())
        self.assertEqual([f"{SERVE} schema={f.n + 2} record=no", f"{DASHBOARD} schema={f.n + 2} record=no"],
                         f.starts())
        self.assertEqual((f.head(f.system), target), f.hub_pin(f.system))

    def recovery_state(self, f, point):
        """The state a kill at `point` of a recovery leaves: the pack moved to
        B, whose second migration fails, and the first committed."""
        self.killed_after_the_move(f)
        f.sd_db("migrate", check=False)
        self.assertEqual(f.n + 1, f.version())
        if point == "before the restore":
            return
        f.git(f.pack, "switch", "-q", "--detach", f.a_pack)
        if point == "after the pack moved back":
            return
        (backup,) = f.backup_dirs()
        f.sd_db("restore", backup)
        self.assertEqual(f.n, f.version())
        if point == "after the restore":
            return
        f.git(f.system, "switch", "-q", "--detach", f.a_system)

    def test_a_kill_at_each_recovery_point_ends_at_the_first_pair(self):
        for point in ("before the restore", "after the pack moved back", "after the restore",
                      "before the record delete"):
            with self.subTest(point=point):
                f = MigrateFixture()
                self.addCleanup(f.destroy)
                f.upstream_migrations(f.good("one"), f.bad("two"))
                self.recovery_state(f, point)
                f.run("refresh", expect=1)
                self.assert_at_first_pair(f)

    def test_a_kill_during_recovery_with_a_fixed_target_ends_at_the_target(self):
        f = self.f
        f.upstream_migrations(f.good("one"), f.bad("two"))
        self.recovery_state(f, "after the pack moved back")
        target = f.upstream_migrations(f.good("two"), replace=True)
        f.run("refresh", expect=0)
        self.assertEqual(f.n + 2, f.version())
        self.assertEqual(target, f.head(f.pack))
        self.assertFalse(f.record.exists())
        self.assertEqual(1, len(f.backup_dirs()))
        self.assertEqual((f.head(f.system), target), f.hub_pin(f.system))

    def test_a_rerun_after_a_failed_restore_takes_no_backup_and_starts_nothing_early(self):
        f = self.f
        f.upstream_migrations(f.good("one"), f.bad("two"))
        lock = f.home / ".local/share/sd/operation-locks/control.lock"
        lock.parent.mkdir(mode=0o700, exist_ok=True)
        holder = f.hold(lock, 600)
        f.run("refresh", expect=1)
        holder.kill()
        holder.wait()
        holder.stdout.close()
        self.assertTrue(f.record.exists())
        f.run("refresh", expect=1)
        self.assert_at_first_pair(f)

    def test_a_kill_after_the_record_delete_starts_and_pushes_on_rerun(self):
        f = self.f
        target = f.upstream_migrations(f.good("one"))
        # A start that fails leaves what a kill after the record delete
        # leaves: migrated, no record, the services down.
        f.run("refresh", expect=1, extra_env={"LAUNCHCTL_FAIL": f"bootstrap {SERVE}"})
        self.assertFalse(f.record.exists())
        self.assertEqual(f.n + 1, f.version())
        self.assertEqual([], f.loaded())
        self.assertIsNone(f.hub_pin(f.system))
        f.run("refresh", expect=0)
        self.assertEqual([DASHBOARD, SERVE], f.loaded())
        self.assertEqual((f.head(f.system), target), f.hub_pin(f.system))
        self.assertEqual(1, len(f.backup_dirs()))

    def test_a_rerun_at_the_first_pair_with_no_record_takes_a_fresh_backup(self):
        f = self.f
        f.upstream_migrations(f.bad("one"))
        f.run("refresh", expect=1, extra_env={"LAUNCHCTL_FAIL": f"bootstrap {SERVE}"})
        self.assertFalse(f.record.exists())
        self.assertEqual([], f.loaded())
        (first,) = f.backup_dirs()
        f.run("refresh", expect=1)
        backups = f.backup_dirs()
        self.assertEqual(2, len(backups))
        second = next(b for b in backups if b != first)
        self.assertEqual(f.record_text(f.a_system, f.a_pack, f.n, second), f.record_seen.read_text())
        self.assertEqual(f.n, f.version())
        self.assertEqual([DASHBOARD, SERVE], f.loaded())

    # --- hub-pin -------------------------------------------------------------

    def test_a_version_behind_before_the_push_pushes_no_hub_pin(self):
        f = self.f
        f.upstream_migrations(f.good("one"))
        behind = f'sqlite3 "$REPO_SYNC_SD_DB" "PRAGMA user_version = {f.n}"'
        result = f.run("refresh", expect=1, extra_env={"LAUNCHCTL_HOOK": behind})
        self.assertIn(f"hub-pin not published: the database reads schema {f.n}, the pack's lib/ {f.n + 1}",
                      result.stdout)
        self.assertIsNone(f.hub_pin(f.system))

    def test_a_failed_push_then_a_new_migration_takes_a_fresh_backup(self):
        f = self.f
        f.upstream_migrations(f.good("one"))
        f.wrap_git("before", "*push*hub-pin*", 'echo "push refused" >&2; exit 1')
        f.run("refresh", expect=1)
        self.assertEqual(f.n + 1, f.version())
        self.assertFalse(f.record.exists())
        self.assertEqual([DASHBOARD, SERVE], f.loaded())
        self.assertIsNone(f.hub_pin(f.system))
        (first,) = f.backup_dirs()
        (f.bin / "git").write_text(GIT_WRAPPER)
        b = f.head(f.pack)
        target = f.upstream_migrations(f.good("two"))
        f.run("refresh", expect=0)
        backups = f.backup_dirs()
        self.assertEqual(2, len(backups))
        second = next(d for d in backups if d != first)
        self.assertEqual(f.record_text(f.head(f.system), b, f.n + 1, second), f.record_seen.read_text())
        self.assertEqual(f.n + 2, f.version())
        self.assertEqual((f.head(f.system), target), f.hub_pin(f.system))

    # --- older state ---------------------------------------------------------

    def test_a_pack_left_ahead_of_the_database_by_an_older_refresh_is_migrated(self):
        """The previous repo-sync moved the pack and printed the migrate steps."""
        f = self.f
        target = f.upstream_migrations(f.good("one"))
        f.git(f.pack, "fetch", "-q", "origin")
        f.git(f.pack, "switch", "-q", "--detach", target)
        f.run("refresh", expect=0)
        (backup,) = f.backup_dirs()
        self.assertEqual(f.record_text(f.a_system, target, f.n, backup), f.record_seen.read_text())
        self.assertEqual(f.n + 1, f.version())
        self.assertEqual((f.head(f.system), target), f.hub_pin(f.system))


if __name__ == "__main__":
    unittest.main()
