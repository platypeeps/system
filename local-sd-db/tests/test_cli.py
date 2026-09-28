"""`sd-db.sh` — the entrypoint the repository's convention asks for."""

import os
import re
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db.schema import SCHEMA_VERSION

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = PACKAGE_ROOT / "sd-db.sh"

REGISTRY = """\
bills:
  anthropic: { cost: subscription }
  openai:    { cost: subscription }
providers:
  claude: { start: "claude -p", vendor: anthropic, bill: anthropic, roles: [author, reviewer] }
  codex:  { start: "codex exec", vendor: openai, bill: openai, roles: [author, reviewer] }
roles:
  author:   [claude, codex]
  reviewer: [codex, claude]
"""


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local/share/sd").mkdir(parents=True)

    def sd_db(self, *args, expect=0, cwd=None, unset=(), **extra):
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment.pop("PYTHONPATH", None)
        # What a case tests the absence of must be absent: CI exports PYTHON
        # in the shell that runs this suite, and an inherited one skips the
        # very fallback a case names (sd:1360).
        for name in unset:
            environment.pop(name, None)
        # The real one may point at the machine's own installed pack, and a
        # verb that reads it would pass or fail on what the operator
        # installed this morning.
        environment.pop("XDG_STATE_HOME", None)
        completed = subprocess.run(
            [str(ENTRYPOINT), *args], capture_output=True, text=True, input="",
            # `work register` takes no repository argument: the checkout is
            # the one enclosing the working directory, so a test of it has to
            # be able to say where it is standing.
            cwd=None if cwd is None else str(cwd),
            env={**environment, "PYTHONPATH": str(PACKAGE_ROOT), **extra},
        )
        self.assertEqual(completed.returncode, expect, completed.stdout + completed.stderr)
        return completed


class WhichInterpreterItRuns(CliCase):
    """A defect that shipped: every verb needed a login shell's PATH.

    `PYTHON="${PYTHON:-python3}"` resolves to Xcode's 3.9 on a bare PATH, and
    `sd_db/backup.py` imports `datetime.UTC`, which 3.9 does not have. So
    every verb here died three imports deep rather than in a sentence. It
    survived because a login shell puts Homebrew first; it broke the first
    time `local-sd-plan` ran `work register` from the runner's exec'd
    environment, which carries `/usr/bin:/bin:/usr/sbin:/sbin` and nothing
    else. `local-sd-plan/sd-plan.sh` hit the same thing first
    (source:local-sd-plan/sd-plan.sh::python_is_new_enough).
    """

    def old_python(self):
        """A stand-in that fails the version probe, on any machine.

        Fabricated rather than reaching for `/usr/bin/python3`, so the case
        runs identically in CI and never turns into a skip -- the CI wrapper
        fails on skips.
        """
        path = self.home / "old-python"
        path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_an_interpreter_that_is_too_old_refuses_in_a_sentence(self):
        done = self.sd_db("init", expect=1, PYTHON=str(self.old_python()))
        self.assertIn("too old", done.stderr)
        self.assertIn("3.13", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_a_bare_path_still_finds_a_usable_interpreter(self):
        """The runner's environment, reproduced.

        This is the invocation that failed: `work register` reached from a
        queued command, with no Homebrew on PATH. `SD_DB_PYTHON` names the
        interpreter running this suite, which is the mechanism the fix relies
        on.
        """
        marker = self.home / "fallback-reached"
        python = self.home / "sd-db-python"
        python.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\n'
                          f'exec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        done = self.sd_db("init", PATH="/usr/bin:/bin", SD_DB_PYTHON=str(python), unset=("PYTHON",))
        self.assertTrue((self.home / ".local/share/sd/sd.db").exists(),
                        "the verb has to have actually run, not just resolved")
        self.assertNotIn("Traceback", done.stdout + done.stderr)
        # An inherited PYTHON takes the early branch and never reaches the
        # fallback loop, so the case passed in CI while testing nothing.
        self.assertTrue(marker.exists(), "the interpreter did not come from the fallback loop")

    def test_the_minimum_version_is_stated_once_and_is_three_thirteen(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertEqual(text.count("(3, 13)"), 1)
        self.assertIn('requires-python = ">=3.13"',
                      (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_a_python_older_than_the_package_metadata_is_refused(self):
        """3.12 passed the old 3.11 probe while pyproject requires 3.13."""
        python = self.home / "python-3.12"
        python.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = -c ]; then\n'
            f'  exec {shlex.quote(sys.executable)} -c '
            '"import sys; sys.version_info = (3, 12, 0, \'final\', 0); exec(sys.argv[1])" "$2"\n'
            'fi\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        done = self.sd_db("init", expect=1, PYTHON=str(python))
        self.assertIn("too old", done.stderr)
        self.assertFalse((self.home / ".local/share/sd/sd.db").exists())

    def test_a_python_that_does_not_exist_is_not_called_too_old(self):
        done = self.sd_db("init", expect=1, PYTHON=str(self.home / "no-such-python"))
        self.assertIn("not an executable command", done.stderr)
        self.assertNotIn("too old", done.stderr)

    def test_help_and_usage_need_no_interpreter(self):
        """The help contract holds on a machine with no usable Python."""
        done = self.sd_db("--help", PYTHON=str(self.old_python()))
        self.assertIn("Usage: sd-db.sh", done.stderr)
        done = self.sd_db(expect=1, PYTHON=str(self.old_python()))
        self.assertIn("Usage: sd-db.sh", done.stderr)
        self.assertNotIn("too old", done.stderr)


class ACheckoutBehindTheDatabase(CliCase):
    """sd:1664: the checkout lags `origin/main`, the database does not.

    `sd-db.sh` runs `sd_db` from this checkout, so one schema bump on `main`
    broke every verb until someone pulled. The refusal told the operator to
    install into a virtualenv this entrypoint never uses. It has to name the
    checkout and the pull, and leave the newer database exactly as it was.
    """

    def sd_db(self, *args, **extra):
        # The checkout's own refusal: an installed copy built for the newer
        # schema would answer instead (sd:1765, tests/test_library_choice.py).
        return super().sd_db(*args, **{"SD_DB_LIBRARY": "checkout", **extra})

    def setUp(self):
        super().setUp()
        self.sd_db("init")
        self.database = self.home / ".local/share/sd/sd.db"
        self.newer = SCHEMA_VERSION + 1
        raw = sqlite3.connect(self.database, isolation_level=None)
        raw.execute(f"PRAGMA user_version = {self.newer}")
        raw.close()

    def version(self):
        raw = sqlite3.connect(self.database, isolation_level=None)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def assert_names_the_pull(self, text):
        self.assertIn(f"schema version {self.newer}", text)
        self.assertIn(f"built for {SCHEMA_VERSION}", text)
        self.assertIn(f"git -C {PACKAGE_ROOT.parent} pull --ff-only", text)
        self.assertNotIn("into this virtualenv", text)
        self.assertNotIn("Traceback", text)

    def test_every_kind_of_verb_refuses_naming_the_pull(self):
        for verb in (("status",), ("repo", "list"), ("usage",), ("migrate",), ("init",)):
            with self.subTest(verb=verb):
                done = self.sd_db(*verb, expect=1)
                self.assert_names_the_pull(done.stderr)
        self.assertEqual(self.version(), self.newer, "a newer database must never move")

    def test_the_backup_mail_names_the_pull_too(self):
        mail = self.home / "mail"
        notify = self.home / "notify"
        notify.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {shlex.quote(str(mail))}\n',
                          encoding="utf-8")
        notify.chmod(0o755)
        done = self.sd_db("backup", "--destination", str(self.home / "backups"),
                          expect=1, SD_NOTIFY=str(notify))
        self.assert_names_the_pull(done.stderr)
        self.assert_names_the_pull(mail.read_text(encoding="utf-8"))
        self.assertEqual(self.version(), self.newer)


class TheTestAndCheckArguments(CliCase):
    """What `test` and `check` hand to unittest, seen through a stub interpreter."""

    def run_stub(self, *args, expect=0):
        python = self.home / "argv-python"
        python.write_text('#!/bin/sh\n[ "$1" = -c ] && exit 0\nprintf "%s\\n" "$@"\n',
                          encoding="utf-8")
        python.chmod(0o755)
        return self.sd_db(*args, expect=expect, PYTHON=str(python))

    def argv(self, *args):
        return self.run_stub(*args).stdout.splitlines()

    def test_a_discovery_option_reaches_discover(self):
        argv = self.argv("test", "-k", "TheCleanReport")
        self.assertEqual(argv[:3], ["-m", "unittest", "discover"])
        self.assertEqual(argv[-2:], ["-k", "TheCleanReport"])

    def test_a_test_id_runs_without_discover(self):
        self.assertEqual(self.argv("test", "tests.test_cli.TheConventions", "-v"),
                         ["-m", "unittest", "tests.test_cli.TheConventions", "-v"])

    def test_check_refuses_an_argument_rather_than_narrow_the_suite(self):
        done = self.run_stub("check", "-k", "x", expect=1)
        self.assertIn("sd-db check: takes no arguments", done.stderr)
        self.assertEqual(done.stdout, "")


class TheFailureMailWithoutNotifyOnPath(CliCase):
    """A PATH without bin-links' `notify` still mails a failed backup."""

    def test_the_vendored_notifier_carries_the_mail(self):
        tree = self.home / "tree"
        (tree / "local-sd-db").mkdir(parents=True)
        (tree / "local-notify").mkdir()
        entrypoint = tree / "local-sd-db/sd-db.sh"
        entrypoint.write_bytes(ENTRYPOINT.read_bytes())
        entrypoint.chmod(0o755)
        record = self.home / "mail"
        notifier = tree / "local-notify/notify.sh"
        notifier.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {shlex.quote(str(record))}\n',
                            encoding="utf-8")
        notifier.chmod(0o755)
        # HOME has no database, so the backup fails whether or not `sd_db` imports.
        environment = {"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                       "PYTHON": sys.executable}
        done = subprocess.run([str(entrypoint), "backup"], capture_output=True, text=True,
                              input="", env=environment, check=False)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertNotIn("the failure mail did not leave", done.stderr)
        self.assertIn("sd-db backup FAILED", record.read_text(encoding="utf-8"))


class TheConventions(CliCase):
    def test_it_is_posix_sh_and_resolves_its_own_folder(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("set -e", text)
        self.assertIn('DIR="$(cd "$(dirname "$0")" && pwd)"', text)

    def test_it_is_named_after_the_folder_without_the_prefix(self):
        self.assertEqual(ENTRYPOINT.name, PACKAGE_ROOT.name.replace("local-", "") + ".sh")

    def test_no_argument_prints_usage_to_stderr_and_exits_one(self):
        completed = self.sd_db(expect=1)
        self.assertEqual(completed.stdout, "")
        self.assertIn("Usage: sd-db.sh", completed.stderr)

    def test_help_describes_the_subcommands_and_exits_zero(self):
        """Each verb starts a line of its own, not merely appears somewhere.

        `assertIn` passed for `retire` while the usage described the verb in
        prose and named no such entry -- the word was in the text, so the
        check agreed with itself. The entries are what the reader scans.

        The verbs come from the dispatcher rather than from a list written
        here. A list written here is one more inventory to remember, and
        `work` was added to the dispatcher and to the usage while this tuple
        still named ten verbs and agreed with itself about all of them.
        """
        completed = self.sd_db("--help")
        entries = {
            match.group(1)
            for match in re.finditer(r"^  ([a-z]+)\b", completed.stderr, re.MULTILINE)
        }
        self.assertTrue(self.dispatched, "read no verbs out of the dispatcher")
        for verb in self.dispatched:
            self.assertIn(verb, entries, f"{verb} is dispatched and undocumented")

    #: Every label of the entrypoint's `case`, minus the catch-all and the
    #: help aliases, which describe no verb of their own.
    @property
    def dispatched(self):
        body = ENTRYPOINT.read_text(encoding="utf-8").partition("\ncase ")[2]
        labels = re.findall(r"^    ([a-z|*-]+)\)$", body, re.MULTILINE)
        return sorted(
            verb
            for label in labels
            for verb in label.split("|")
            if verb not in ("*", "-h", "--help", "help")
        )


class TheVerbs(CliCase):
    def test_init_creates_the_database_and_seeds_the_registry(self):
        (self.home / ".local/share/sd/providers.yaml").write_text(REGISTRY, encoding="utf-8")
        completed = self.sd_db("init")
        self.assertIn(f"schema version {SCHEMA_VERSION}", completed.stdout)
        self.assertIn("seeded 4 row(s)", completed.stdout)
        self.assertTrue((self.home / ".local/share/sd/sd.db").is_file())

    def test_init_without_a_registry_says_so_rather_than_failing(self):
        completed = self.sd_db("init")
        self.assertIn("providers.yaml yet", completed.stdout)
        # The line promises what the library keeps: a writable read seeds.
        # It used to say "will seed on first read" while no read seeded.
        self.assertIn("writable connection", completed.stdout)
        self.assertNotIn("will seed on first read", completed.stdout)

    def test_a_second_init_reports_the_seed_without_duplicating_it(self):
        (self.home / ".local/share/sd/providers.yaml").write_text(REGISTRY, encoding="utf-8")
        self.assertIn("seeded 4 row(s)", self.sd_db("init").stdout)
        again = self.sd_db("init")
        self.assertIn("already seeded", again.stdout)
        self.assertNotIn("seeded 4 row(s)", again.stdout)
        connection = sqlite3.connect(self.home / ".local/share/sd/sd.db")
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM provider").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM bill").fetchone()[0], 2)
        finally:
            connection.close()

    def test_init_before_the_file_then_a_writable_read_seeds(self):
        """The shape the finding came from: `init` first, the file after."""
        self.sd_db("init")
        (self.home / ".local/share/sd/providers.yaml").write_text(REGISTRY, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-c",
             "import os, sd_db\n"
             "c = sd_db.connect(home=os.environ['HOME'])\n"
             "sd_db.read_registry(home=os.environ['HOME'], connection=c)\n"
             "print(c.execute('SELECT count(*) FROM provider').fetchone()[0])"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(self.home), "PYTHONPATH": str(PACKAGE_ROOT)},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "2")

    def test_migrate_twice_reports_the_second_run_as_nothing(self):
        self.sd_db("init")
        again = self.sd_db("migrate")
        self.assertIn("nothing applied", again.stdout)

    def test_status_before_init_says_what_to_run(self):
        completed = self.sd_db("status", expect=1)
        self.assertIn("sd-db.sh init", completed.stdout)

    def test_status_names_both_versions(self):
        self.sd_db("init")
        completed = self.sd_db("status")
        self.assertIn(
            f"schema version {SCHEMA_VERSION}, this library is built for "
            f"{SCHEMA_VERSION}",
            completed.stdout,
        )
        from sd_db.schema import TABLES
        self.assertIn(f"{len(TABLES)} table(s)", completed.stdout)

    def test_status_reads_a_database_awaiting_a_migration(self):
        """The read path must work while the write path refuses."""
        self.sd_db("init")
        raw = sqlite3.connect(self.home / ".local/share/sd/sd.db", isolation_level=None)
        raw.execute("PRAGMA user_version = 0")
        raw.close()
        completed = self.sd_db("status")
        self.assertIn(
            f"schema version 0, this library is built for {SCHEMA_VERSION}",
            completed.stdout,
        )

    def test_restore_reports_the_unreconciled_record(self):
        self.sd_db("init")
        self.sd_db("backup")
        dated = sorted((self.home / "Documents/sd-backups").iterdir())[-1]
        completed = self.sd_db("restore", str(dated))
        self.assertIn("unreconciled", completed.stdout)
        status = self.sd_db("status")
        self.assertRegex(status.stdout, re.compile(r"unresolved restore", re.MULTILINE))



class MigrationVerbCase(CliCase):
    """A database, and a checkout with one item in it, through the entrypoint.

    The shell script is what a person and a cron job actually run, and a verb
    wired into the Python side but not into the `case` is a verb that only
    the tests can reach.
    """

    def item(self):
        return self.checkout / "docs/work/2026-07-01-an-item/prd.md"

    def setUp(self):
        super().setUp()
        self.sd_db("init")
        self.checkout = self.home / "checkout"
        self.checkout.mkdir()
        for argv in (
            ["init", "-q", "-b", "main", "."],
            ["config", "user.email", "fixture@example.invalid"],
            ["config", "user.name", "Fixture"],
        ):
            subprocess.run(["git", "-C", str(self.checkout), *argv], check=True,
                           capture_output=True)
        item = self.checkout / "docs/work/2026-07-01-an-item/prd.md"
        item.parent.mkdir(parents=True)
        item.write_text(
            "---\ntitle: an item\nstatus: planning\ncreated: 2026-07-01\n---\n\nbody\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.checkout), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.checkout), "commit", "-qm", "an item"],
                       check=True, capture_output=True)


class TheMigrationVerbs(MigrationVerbCase):
    def test_repo_list_on_an_empty_table_says_what_to_run(self):
        completed = self.sd_db("repo", "list", expect=1)
        self.assertIn("repo seed", completed.stdout)

    def test_repo_add_then_list_shows_the_checkout(self):
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("repo", "list")
        self.assertIn("checkout", completed.stdout)

    def test_repo_runner_merge_sets_the_column_and_list_shows_it(self):
        """sd:1131: the operator interface the column never had.

        `list` is where the answer is read back, so it carries the setting
        too; before this the only way to see it was to open the database.
        """
        self.sd_db("repo", "add", str(self.checkout))
        self.assertIn("manual", self.sd_db("repo", "list").stdout)
        completed = self.sd_db("repo", "runner-merge", str(self.checkout), "auto")
        self.assertIn("runner_merge manual -> auto", completed.stdout)
        self.assertIn("auto", self.sd_db("repo", "list").stdout)

    def test_repo_runner_merge_refuses_an_unknown_value_and_an_unknown_path(self):
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("repo", "runner-merge", str(self.checkout), "automatic", expect=1)
        self.assertIn("manual or auto", completed.stdout + completed.stderr)
        completed = self.sd_db("repo", "runner-merge", str(self.home / "absent"), "auto", expect=1)
        self.assertIn("not a registered repository", completed.stdout + completed.stderr)
        self.assertIn("manual", self.sd_db("repo", "list").stdout)

    def _list_row(self, *extra: str) -> list[str]:
        lines = [line for line in self.sd_db("repo", "list", *extra).stdout.splitlines()
                 if "checkout" in line]
        self.assertEqual(len(lines), 1, lines)
        return lines[0].split()

    def test_repo_managed_sets_the_flag_and_list_shows_it_before_runner_merge(self):
        """sd:1619. `managed` sits immediately before `runner_merge`; `ci`
        (sd:1843) follows them as the last field."""
        self.sd_db("repo", "add", str(self.checkout))
        self.assertEqual(self._list_row()[-3:-1], ["no", "manual"])
        completed = self.sd_db("repo", "managed", str(self.checkout), "yes")
        self.assertIn("managed no -> yes", completed.stdout)
        self.assertEqual(self._list_row()[-3:-1], ["yes", "manual"])
        self.sd_db("repo", "runner-merge", str(self.checkout), "auto")
        self.assertEqual(self._list_row()[-3:-1], ["yes", "auto"])

    def test_repo_ci_sets_the_mode_and_list_shows_it_as_the_last_field(self):
        """sd:1843. `ci` is appended, so every field before it keeps its
        position, and the verb round-trips through `list`."""
        self.sd_db("repo", "add", str(self.checkout))
        self.assertEqual(self._list_row()[-4:], ["file", "no", "manual", "github"])
        completed = self.sd_db("repo", "ci", str(self.checkout), "local")
        self.assertIn("ci github -> local", completed.stdout)
        self.assertEqual(self._list_row()[-4:], ["file", "no", "manual", "local"])
        completed = self.sd_db("repo", "ci", str(self.checkout), "github")
        self.assertIn("ci local -> github", completed.stdout)
        self.assertEqual(self._list_row()[-1], "github")

    def test_repo_ci_refuses_a_bad_value_an_unknown_path_and_a_missing_argument(self):
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("repo", "ci", str(self.checkout), "actions", expect=1)
        self.assertIn("github or local", completed.stdout + completed.stderr)
        completed = self.sd_db("repo", "ci", str(self.home / "absent"), "local", expect=1)
        self.assertIn("not a registered repository", completed.stdout + completed.stderr)
        completed = self.sd_db("repo", "ci", str(self.checkout), expect=1)
        self.assertIn("needs a path and github or local", completed.stderr)
        self.assertEqual(self._list_row()[-1], "github")

    def test_repo_managed_refuses_a_bad_value_an_unknown_path_and_a_missing_argument(self):
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("repo", "managed", str(self.checkout), "true", expect=1)
        self.assertIn("yes or no", completed.stdout + completed.stderr)
        completed = self.sd_db("repo", "managed", str(self.home / "absent"), "yes", expect=1)
        self.assertIn("not a registered repository", completed.stdout + completed.stderr)
        completed = self.sd_db("repo", "managed", str(self.checkout), expect=1)
        self.assertIn("needs a path and yes or no", completed.stderr)
        self.assertEqual(self._list_row()[-3], "no")

    def test_repo_list_managed_prints_only_the_managed_rows(self):
        other = self.home / "other"
        subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
        self.sd_db("repo", "add", str(self.checkout))
        self.sd_db("repo", "add", str(other))
        self.sd_db("repo", "managed", str(self.checkout), "yes")
        listed = self.sd_db("repo", "list", "--managed").stdout
        self.assertIn("checkout", listed)
        self.assertNotIn("other", listed)
        self.assertIn("other", self.sd_db("repo", "list").stdout)

    def test_repo_list_managed_with_none_marked_says_so_and_succeeds(self):
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("repo", "list", "--managed")
        self.assertNotIn("checkout", completed.stdout)
        self.assertIn("repo managed PATH yes", completed.stderr)

    def test_repo_list_refuses_an_unknown_option(self):
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("repo", "list", "--manged", expect=1)
        self.assertIn("--manged", completed.stderr)

    def test_import_docs_work_lands_rows_and_points_at_the_retire_verb(self):
        """`import` still retires nothing, and now names the verb that does.

        The line it used to print said the source was "unchanged and still
        authoritative", which stopped being true of `docs/work` the moment
        the retire step landed. A printed line that outlives what it
        describes is the same defect as a comment that does.
        """
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("import", "docs-work")
        self.assertIn("1 seen, 1 inserted", completed.stdout)
        self.assertIn("nothing retired here", completed.stdout)
        self.assertIn("sd-db.sh retire docs-work", completed.stdout)
        self.assertNotIn("still authoritative", completed.stdout)

    def test_a_second_import_reports_the_same_counts_with_zero_new_rows(self):
        self.sd_db("repo", "add", str(self.checkout))
        self.sd_db("import", "docs-work")
        completed = self.sd_db("import", "docs-work")
        self.assertIn("1 seen, 0 inserted", completed.stdout)
        self.assertIn("1 unchanged", completed.stdout)

    def test_verify_agrees_after_an_import(self):
        self.sd_db("repo", "add", str(self.checkout))
        self.sd_db("import", "docs-work")
        completed = self.sd_db("verify", "docs-work")
        self.assertIn("agree with the rows", completed.stdout)

    def test_verify_names_the_difference_and_exits_one(self):
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("verify", "docs-work", expect=1)
        self.assertIn("missing from the rows", completed.stderr)

    def test_an_unknown_source_names_the_five(self):
        completed = self.sd_db("import", "nowhere", expect=1)
        self.assertIn("docs-work", completed.stderr)
        self.assertIn("vault", completed.stderr)


class ARetiredRepositoryIsANoOp(MigrationVerbCase):
    """A repository that has retired is the intended end state, not a fault.

    From the last retire until 2026-09-11 both verbs exited 1 on every run:
    the whole-tree reader met a retired repository first, refused, and the
    eleven others were never read. The verbs now leave a retired repository
    out and say so by name, read whatever is still on the file source, and
    exit 0 when nothing they read failed. The fixture is two checkouts, one
    retired the way `retire` leaves one -- lines gone, marker committed --
    and one still on `file` with its line in place.
    """

    def setUp(self):
        super().setUp()
        # `repo add` records the key of the resolved path, `~/` and the path
        # under the home (sd:1439), and the lines name that.
        self.checkout = self.checkout.resolve()
        self.retired = self.home.resolve() / "retired"
        self.checkout_key = "~/" + self.checkout.relative_to(self.home.resolve()).as_posix()
        self.retired_key = "~/retired"
        self.retired.mkdir()
        for argv in (
            ["init", "-q", "-b", "main", "."],
            ["config", "user.email", "fixture@example.invalid"],
            ["config", "user.name", "Fixture"],
        ):
            subprocess.run(["git", "-C", str(self.retired), *argv], check=True,
                           capture_output=True)
        item = self.retired / "docs/work/2026-07-02-retired-item/prd.md"
        item.parent.mkdir(parents=True)
        item.write_text(
            "---\ntitle: a retired item\ncreated: 2026-07-02\n---\n\nbody\n",
            encoding="utf-8",
        )
        (self.retired / "docs/work/.status-source").write_text("row\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.retired), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.retired), "commit", "-qm", "retired"],
                       check=True, capture_output=True)
        self.sd_db("repo", "add", str(self.checkout))
        self.sd_db("repo", "add", str(self.retired))

    def switch(self, checkout):
        """The row alone says `row`, as a retire killed before its commit leaves it."""
        raw = sqlite3.connect(self.home / ".local/share/sd/sd.db", isolation_level=None)
        try:
            raw.execute("UPDATE repo SET status_source = 'row' WHERE path = ?",
                        (checkout,))
        finally:
            raw.close()

    def test_import_reports_the_retired_one_by_name_and_imports_the_other(self):
        completed = self.sd_db("import", "docs-work")
        self.assertIn(f"{self.retired_key} was retired", completed.stdout)
        self.assertIn("read the row, not the file", completed.stdout)
        self.assertIn("1 seen, 1 inserted", completed.stdout)
        self.assertNotIn("all retired", completed.stdout)
        raw = sqlite3.connect(self.home / ".local/share/sd/sd.db")
        try:
            repos = sorted(row[0] for row in raw.execute("SELECT repo FROM item"))
        finally:
            raw.close()
        self.assertEqual(repos, [self.checkout_key])

    def test_verify_leaves_the_retired_one_out_and_agrees_on_the_other(self):
        self.sd_db("import", "docs-work")
        completed = self.sd_db("verify", "docs-work")
        self.assertIn(f"{self.retired_key} was retired", completed.stdout)
        self.assertIn("1 record(s) agree with the rows", completed.stdout)

    def test_a_row_that_says_row_is_retired_even_with_its_lines_in_place(self):
        """The retire switches the row before it commits, so a killed one
        leaves lines behind that a row already answers for. An import that
        read them would write the stale words back over the row."""
        self.sd_db("import", "docs-work")
        self.switch(self.checkout_key)
        completed = self.sd_db("import", "docs-work")
        self.assertIn(f"{self.checkout_key} was retired", completed.stdout)
        self.assertIn(f"{self.retired_key} was retired", completed.stdout)
        self.assertNotIn("seen", completed.stdout)

    def test_a_fleet_with_every_repository_retired_says_so_and_exits_zero(self):
        self.switch(self.checkout_key)
        for verb in ("import", "verify"):
            completed = self.sd_db(verb, "docs-work")
            self.assertEqual(
                completed.stdout.count("was retired"), 2, completed.stdout
            )
            self.assertIn(
                f"docs-work: 2 repositories, all retired; nothing to {verb}",
                completed.stdout,
            )
            self.assertIn("sd-db.sh work register", completed.stdout)
            self.assertEqual(completed.stderr, "")

    def test_a_failing_file_repository_still_exits_one(self):
        """The retired one is a no-op; the one still on `file` is not excused."""
        self.item().write_text(
            "---\ntitle: an item\nstatus: nonsense\ncreated: 2026-07-01\n---\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.checkout), "commit", "-qam", "broken"],
                       check=True, capture_output=True)
        completed = self.sd_db("import", "docs-work", expect=1)
        self.assertIn("'nonsense'", completed.stderr)
        # The failure leaves the sitting; the retired one is still reported.
        self.assertIn(f"{self.retired_key} was retired", completed.stdout)

    def test_a_verify_whose_freeze_fails_still_names_the_retired_one(self):
        self.item().write_text(
            "---\ntitle: an item\nstatus: nonsense\ncreated: 2026-07-01\n---\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.checkout), "commit", "-qam", "broken"],
                       check=True, capture_output=True)
        completed = self.sd_db("verify", "docs-work", expect=1)
        self.assertIn("'nonsense'", completed.stderr)
        self.assertIn(f"{self.retired_key} was retired", completed.stdout)


class TheRetireVerb(MigrationVerbCase):
    """`retire` through the entrypoint: wired into the `case`, not only Python.

    A verb reachable from `sd_db.jobs.cli` and missing from the shell script
    is a verb only the tests can run, which is convention 1's whole point.
    """

    def pack(self, **kwargs):
        from . import support

        return support.pack(self.home, **kwargs)

    def test_it_refuses_under_a_pack_that_cannot_read_the_row(self):
        """The version installed here is `eb7695c7`'s shape: `delivered` and
        no `status_marker`. A `delivered`-only guard would let it through."""
        from . import support

        self.pack(library=support.PACK_BEFORE_THE_ROW_READERS)
        self.sd_db("repo", "add", str(self.checkout))
        self.sd_db("import", "docs-work")
        completed = self.sd_db("retire", "docs-work", expect=1)
        self.assertIn("47d41245a470", completed.stderr)
        self.assertIn("status_marker", completed.stderr)
        self.assertIn("status: planning", self.item().read_text(encoding="utf-8"))

    def test_it_refuses_with_no_pack_installed_at_all(self):
        self.sd_db("repo", "add", str(self.checkout))
        self.sd_db("import", "docs-work")
        completed = self.sd_db("retire", "docs-work", expect=1)
        self.assertIn("no receipt", completed.stderr)

    def test_it_refuses_before_the_source_has_ever_been_imported(self):
        self.pack()
        self.sd_db("repo", "add", str(self.checkout))
        completed = self.sd_db("retire", "docs-work", expect=1)
        self.assertIn("never been verified", completed.stderr)
        self.assert_runnable(completed.stderr)

    def assert_runnable(self, text):
        """Every `sd-db.sh ...` a message prints has to be a command that runs.

        A source is named for what it reads -- `docs/work` -- and the CLI takes
        no slashes, so a refusal that interpolated the source's own name told
        the operator to run `sd-db.sh import docs/work`, which the CLI rejects.
        Checked against the registries themselves rather than a list written
        here, so a source or verb added later is covered by existing.
        """
        from sd_db.jobs.cli import COMMANDS, SOURCES

        found = re.findall(r"`sd-db\.sh ([a-z-]+)(?: ([^`\s]+))?`", text)
        self.assertTrue(found, f"no `sd-db.sh ...` command in: {text}")
        for verb, argument in found:
            self.assertIn(verb, COMMANDS, f"`sd-db.sh {verb}` is not a verb")
            if argument:
                self.assertIn(argument, SOURCES, f"`{argument}` is not a source")

    def test_it_removes_the_line_writes_the_marker_and_sets_the_row(self):
        self.pack()
        self.sd_db("repo", "add", str(self.checkout))
        self.sd_db("import", "docs-work")
        completed = self.sd_db("retire", "docs-work")
        self.assertIn("1 line(s) removed, 0 archived line(s) kept", completed.stdout)
        self.assertIn("now reads status from the row", completed.stdout)
        self.assertNotIn("status:", self.item().read_text(encoding="utf-8"))
        self.assertEqual(
            (self.checkout / "docs/work/.status-source").read_text(encoding="utf-8"),
            "row\n",
        )
        listed = self.sd_db("repo", "list")
        # Status source, managed, runner merge, then ci: the four trailing
        # fields `repo list` prints.
        self.assertEqual(listed.stdout.split()[-4:], ["row", "no", "manual", "github"])

    def test_it_needs_a_source(self):
        completed = self.sd_db("retire", expect=1)
        self.assertIn("needs one of", completed.stderr)
        self.assertIn("docs-work", completed.stderr)


# The remove-verb cases' own imports sit here, below the two line-keyed
# citations in `WhichInterpreterItRuns`, so those lines keep their numbers in
# `tests/test_citations.py`.
import json  # noqa: E402
import shlex  # noqa: E402
import signal  # noqa: E402
import textwrap  # noqa: E402
import uuid  # noqa: E402
from unittest import mock  # noqa: E402

STAMP = "2026-09-09T12:00:00+00:00"

#: `sd_db.jobs.cli.main` in a child of its own, so a real signal reaches
#: real handlers (`implement.md` step 6). The patches are applied inside the
#: child: `backup.run` raises the signal before the real backup, the
#: `transaction` wrapper raises it as the commit returns, and `ingest` can
#: raise `sqlite3.OperationalError`. It prints `main`'s return, then the
#: handler of each of the three signals after `main` returned.
SIGNALLED = textwrap.dedent("""
    import json, signal, sqlite3, sys
    from contextlib import contextmanager
    from unittest import mock
    from sd_db import removal, reporting
    from sd_db.jobs import cli

    number, point, times, argv = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), json.loads(sys.argv[4])
    names = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old = {name: signal.getsignal(name) for name in names}
    real_run, real_transaction = removal.backups.run, removal.transaction

    def run(**kwargs):
        for _ in range(times):
            signal.raise_signal(number)
        return real_run(**kwargs)

    @contextmanager
    def transaction(connection):
        with real_transaction(connection):
            yield connection
        for _ in range(times):
            signal.raise_signal(number)

    patches = []
    if point == "backup":
        patches.append(mock.patch.object(removal.backups, "run", side_effect=run))
    elif point == "commit":
        patches.append(mock.patch.object(removal, "transaction", transaction))
    elif point == "ingest":
        patches.append(mock.patch.object(reporting, "ingest", side_effect=sqlite3.OperationalError("database is locked")))
    for patch in patches:
        patch.start()
    try:
        code = cli.main(argv)
    except Exception as error:
        code = f"raised {type(error).__name__}: {error}"
    finally:
        for patch in patches:
            patch.stop()
    print("returned", code, flush=True)
    print("restored", all(signal.getsignal(name) is old[name] for name in names), flush=True)
    """)

#: A child that holds `runner_journal.lock` on a path until it is killed.
HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from sd_db import runner_journal
    with runner_journal.lock(Path(sys.argv[1])):
        print("held", flush=True)
        time.sleep(60)
    """)


class RemoveCase(CliCase):
    """The probe fixture of `design.md` section 7 in the entrypoint's own home.

    Fifteen rows: a repo, an item with seven notes, two done assignments,
    their two released runs and leases, plus an older item with a newer
    assignment so A1 clears. The runs' journal pairs are written as the
    runner writes them, so the apply's backup accepts the store. The retained
    root exists and holds no clone unless a test makes one.
    """

    def setUp(self):
        super().setUp()
        # The store names its paths resolved (`/private/var`, not `/var`), and
        # so must the ones the assertions build.
        self.home = self.home.resolve()
        self.sd_db("init")
        import sd_db
        from sd_db import runner_journal
        from sd_db.writes import add_note, create_assignment, create_item, upsert_repo

        self.database = self.home / ".local/share/sd/sd.db"
        self.retained = self.home / "retained"; self.retained.mkdir()
        self.db = sd_db.connect(self.database); self.addCleanup(self.db.close)
        self.older = create_item(self.db, kind="task", title="task38-queue-only", status="done")
        create_assignment(self.db, role="author", status="done", item=self.older)
        self.source = str(self.home / "checkouts" / "provisioning-probe")
        upsert_repo(self.db, self.source)
        self.probe = create_item(self.db, kind="task", title="runner provisioning probe", status="done", repo=self.source)
        for kind in ("decision", "exec", "decision", "exec", "decision"):
            if kind == "exec":
                add_note(self.db, self.probe, kind, "{}", session="runner", started=STAMP, ended=STAMP, exit_code=0)
            else:
                add_note(self.db, self.probe, kind, "went fine", session="alex")
        self.db.execute("INSERT INTO note (item, timestamp, kind, body) VALUES (?, ?, 'status_change',"
                        " 'in_progress -> done by alex')", (self.probe, STAMP))
        self.works = [create_assignment(self.db, role="author", status="done", item=self.probe) for _ in range(2)]
        self.runs = []
        for work in self.works:
            ident = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path,"
                " created_at, updated_at, released_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ident, work, 1, self.source, "sd/probe", "runner",
                 str(self.home / "work" / str(self.probe) / f"{work}-1-{ident}"),
                 str(self.retained / str(work) / "1" / "clone"), STAMP, STAMP, STAMP))
            self.db.execute("INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at, released_at)"
                            " VALUES (?,?,?,?,?,?)", (ident, self.source, "sd/probe", 1, STAMP, STAMP))
            self.runs.append(ident)
        create_assignment(self.db, role="author", status="done", item=self.older)
        self.journal = runner_journal.directory(self.database)
        for run in self.runs:
            runner_journal.persist(self.database, dict(self.db.execute("SELECT * FROM runner_run WHERE id=?",
                                                                       (run,)).fetchone()))
        self.pairs = [[self.journal / f"{run}.json", self.journal / f"{run}.lock"] for run in self.runs]
        # One leftover beside the first run's (absent) clone, so the preview has a `left:` section.
        self.leftover = self.retained / str(self.works[0]) / "1" / "retention.json"
        self.leftover.parent.mkdir(parents=True)
        self.leftover.write_text("{}")

    def sd_db(self, *args, expect=0, **extra):
        # `SD_SESSION` is recorded in the record and refused over 200
        # characters, and `SD_REPO_ROOT` points P5 at a conf; neither may
        # leak in from the operator's shell.
        for name in ("SD_SESSION", "SD_REPO_ROOT"):
            extra.setdefault(name, None)
        cleared = {name for name, value in extra.items() if value is None}
        extra = {name: value for name, value in extra.items() if value is not None}
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        for name in ("PYTHONPATH", "XDG_STATE_HOME", *cleared):
            environment.pop(name, None)
        completed = subprocess.run(
            [str(ENTRYPOINT), *args], capture_output=True, text=True, input="",
            env={**environment, "PYTHONPATH": str(PACKAGE_ROOT), **extra},
        )
        self.assertEqual(completed.returncode, expect, completed.stdout + completed.stderr)
        return completed

    def counts(self):
        return {name: self.db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def records(self):
        return [dict(row) for row in self.db.execute("SELECT id, fields, body FROM item WHERE source='cron-report'")]

    def snapshots(self):
        root = self.home / "Documents" / "sd-backups"
        return sorted(root.iterdir()) if root.is_dir() else []

    @staticmethod
    def fingerprint_of(output):
        found = re.findall(r"^sd-db: fingerprint: ([0-9a-f]{64})$", output, re.MULTILINE)
        return found[0] if found else None

    def hold(self, path):
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdout=subprocess.PIPE, text=True,
                                 env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)})
        self.addCleanup(self.kill, child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    @staticmethod
    def kill(child):
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait()


class TheRemoveVerbs(RemoveCase):
    """`item remove` and `repo remove` through the entrypoint (`implement.md` step 6)."""

    def test_the_verbs_refuse_without_who_reason_or_the_fingerprint_before_the_store_opens(self):
        before = self.database.stat().st_mtime_ns
        for argv, flag in ((("item", "remove", str(self.probe), "--reason", "r"), "--who"),
                           (("item", "remove", str(self.probe), "--who", "w"), "--reason"),
                           (("repo", "remove", self.source, "--reason", "r"), "--who"),
                           (("item", "remove", str(self.probe), "--who", "w", "--reason", "r", "--apply"),
                            "--if-fingerprint")):
            with self.subTest(flag=flag):
                completed = self.sd_db(*argv, expect=1)
                self.assertIn(flag, completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(self.database.stat().st_mtime_ns, before)
        self.assertEqual(self.snapshots(), [])

    def test_a_line_break_in_who_reason_or_the_target_is_refused_before_the_store_opens(self):
        """Copilot on #414: `shlex.quote` keeps a line break, and the last line would no longer apply."""
        before = self.database.stat().st_mtime_ns
        for argv, name in ((("item", "remove", str(self.probe), "--who", "a\nb", "--reason", "r"), "--who"),
                           (("item", "remove", str(self.probe), "--who", "w", "--reason", "r\rs"), "--reason"),
                           (("repo", "remove", self.source + "\nx", "--who", "w", "--reason", "r"), "the target")):
            with self.subTest(name=name):
                completed = self.sd_db(*argv, expect=1)
                self.assertIn(f"{name} must be one line", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(self.database.stat().st_mtime_ns, before)

    def test_the_preview_opens_the_store_read_only_and_the_apply_for_write(self):
        """Copilot on #414: a writable open sets the journal mode; a preview changes nothing."""
        from sd_db.jobs import cli

        opened = []
        real = cli.connect

        def connect(path, **kwargs):
            opened.append(kwargs.get("write", True))
            return real(path, **kwargs)

        import io
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch.object(cli, "connect", connect), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(cli.main(["item", "remove", str(self.probe), "--who", "alex", "--reason", "r"]), 0)
            fingerprint = self.fingerprint_of(out.getvalue())
            self.assertEqual(cli.main(["item", "remove", str(self.probe), "--who", "alex", "--reason", "r",
                                       "--apply", "--if-fingerprint", fingerprint]), 0)
        self.assertEqual(opened, [False, True])

    def test_repo_and_item_with_no_verb_name_their_verbs(self):
        completed = self.sd_db("repo", expect=1)
        for verb in ("add", "seed", "list", "runner-merge", "managed", "ci", "remove"):
            self.assertIn(verb, completed.stderr)
        completed = self.sd_db("item", expect=1)
        self.assertIn("remove", completed.stderr)

    def test_a_clean_preview_exits_0_and_its_last_line_applies_as_given(self):
        completed = self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex",
                               "--reason", "the probe is finished")
        lines = completed.stdout.rstrip("\n").split("\n")
        rows = [line for line in lines if re.match(r"^sd-db:   (repo|item|note|assignment|runner_run|runner_lease) ", line)]
        self.assertEqual(len(rows), 15, completed.stdout)
        fingerprint = self.fingerprint_of(completed.stdout)
        self.assertIsNotNone(fingerprint, completed.stdout)
        self.assertIn("sd-db: rows: 15 in 1 note(s)", lines)
        # C-53: the journal files under `move`, before `left`.
        move, left = lines.index("sd-db: move after commit:"), lines.index("sd-db: left:")
        self.assertLess(move, left)
        self.assertEqual(lines[move + 1:move + 6], [f"sd-db:   file {path}" for pair in self.pairs for path in pair]
                         + [f"sd-db:   to {self.database.parent / 'runner-recovery-evidence' / f'removed-{fingerprint}'}"])
        self.assertEqual(lines[left + 1], f"sd-db:   file {self.leftover}")
        self.assertEqual(self.counts()["runner_run"], 2)
        parts = shlex.split(lines[-1])
        self.assertEqual(parts[:4], ["sd-db.sh", "repo", "remove", self.source])
        self.assertEqual(parts[-2:], ["--if-fingerprint", fingerprint])
        applied = self.sd_db(*parts[1:])
        self.assertIn("record item", applied.stdout)
        self.assertEqual(self.counts()["runner_run"], 0)
        self.assertIsNone(self.db.execute("SELECT 1 FROM item WHERE id=?", (self.probe,)).fetchone())
        (record,) = self.records()
        self.assertEqual(json.loads(record["fields"])["record"], "repo-remove")
        actor = json.loads(record["fields"])["report"]["actor"]
        self.assertEqual((actor["who"], actor["reason"], actor["program"]),
                         ("alex", "the probe is finished", "sd-db.sh repo remove"))
        self.assertEqual(actor["principal"], os.environ.get("USER") or actor["principal"])
        self.assertIn(f"backup: {self.snapshots()[0]}", json.loads(record["body"])["text"].split("\n"))
        for path in self.pairs[0] + self.pairs[1]:
            self.assertIn(f"sd-db: moved {path}", applied.stdout)
            self.assertFalse(path.exists())

    def test_a_preview_with_a_refusal_exits_3_and_removes_nothing(self):
        from sd_db.writes import add_note

        note = add_note(self.db, self.probe, "followup", "still open", session="alex")
        before = self.counts()
        completed = self.sd_db("item", "remove", str(self.probe), "--who", "alex", "--reason", "r", expect=3)
        self.assertIn(f"I7 note {note}: note {note} is an unresolved followup", completed.stdout)
        self.assertIsNotNone(self.fingerprint_of(completed.stdout))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.snapshots(), [])
        # The apply with that fingerprint is refused as an error, before any backup.
        fingerprint = self.fingerprint_of(completed.stdout)
        refused = self.sd_db("item", "remove", str(self.probe), "--who", "alex", "--reason", "r",
                             "--apply", "--if-fingerprint", fingerprint, expect=1)
        self.assertIn("I7", refused.stderr)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.snapshots(), [])

    def test_a_retained_clone_is_refused_with_the_retained_remove_command_printed(self):
        """sd:1793: the printed line is the runner's verb, not a raw `chflags` and `rm -rf`."""
        clone = self.retained / str(self.works[0]) / "1" / "clone"
        clone.mkdir(parents=True)
        completed = self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex", "--reason", "r",
                               expect=3)
        lines = completed.stdout.split("\n")
        self.assertIn(f"runner.sh retained-remove --clone-only --assignment {self.works[0]} --who NAME", lines)
        self.assertNotIn("chflags", completed.stdout)
        self.assertNotIn("rm -rf", completed.stdout)
        self.assertIn(str(clone), completed.stdout)
        self.assertTrue(clone.is_dir())
        self.assertIn("P4", completed.stdout)

    def test_an_over_long_sd_session_is_g4_on_both_paths_and_prints_no_plan(self):
        fingerprint = self.fingerprint_of(self.sd_db("item", "remove", str(self.probe), "--who", "alex",
                                                     "--reason", "r").stdout)
        long = "s" * 201
        preview = self.sd_db("item", "remove", str(self.probe), "--who", "alex", "--reason", "r", expect=1,
                             SD_SESSION=long)
        self.assertIn("G4", preview.stderr)
        self.assertIn("session", preview.stderr)
        self.assertIsNone(self.fingerprint_of(preview.stdout))
        self.assertNotIn("removed:", preview.stdout)
        applied = self.sd_db("item", "remove", str(self.probe), "--who", "alex", "--reason", "r",
                             "--apply", "--if-fingerprint", fingerprint, expect=1, SD_SESSION=long)
        self.assertIn("G4", applied.stderr)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.counts()["runner_run"], 2)
        # A session that fits is recorded.
        applied = self.sd_db("item", "remove", str(self.probe), "--who", "alex", "--reason", "r",
                             "--apply", "--if-fingerprint", fingerprint, SD_SESSION="session-7")
        (record,) = self.records()
        self.assertEqual(json.loads(record["fields"])["report"]["actor"]["session"], "session-7")

    def test_item_remove_through_the_entrypoint_and_the_importer_warning(self):
        """C-46: `item` in the entrypoint's `case`, or the verb only the tests can reach."""
        from sd_db.writes import create_item

        other = create_item(self.db, kind="task", title="registered", status="done", source="register",
                            external_id="O27")
        completed = self.sd_db("item", "remove", str(other), "--who", "alex", "--reason", "r")
        self.assertIsNotNone(self.fingerprint_of(completed.stdout))
        self.assertIn(f"item {other} was imported from register as O27; the next import can bring it back",
                      completed.stdout)
        self.assertNotIn("Usage: sd-db.sh", completed.stderr)

    def test_the_backup_verb_passes_on_a_store_that_holds_a_record(self):
        """Step 7: `backup` exits 0 after an apply and reports no `BROKEN:` clause."""
        fingerprint = self.fingerprint_of(self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex",
                                                     "--reason", "r").stdout)
        self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex", "--reason", "r",
                   "--apply", "--if-fingerprint", fingerprint)
        self.assertEqual(len(self.records()), 1)
        completed = self.sd_db("backup")
        self.assertNotIn("BROKEN", completed.stdout + completed.stderr)
        self.assertIn("row(s) across", completed.stdout)
        self.assertEqual(len(self.snapshots()), 2)

    def test_a_held_journal_lock_exits_4_with_the_reason_and_the_lines(self):
        fingerprint = self.fingerprint_of(self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex",
                                                     "--reason", "r").stdout)
        self.hold(self.pairs[0][1])
        completed = self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex", "--reason", "r",
                               "--apply", "--if-fingerprint", fingerprint, expect=4)
        self.assertIn(f"run journal lock is held: {self.pairs[0][1]}", completed.stderr)
        lines = completed.stdout.split("\n")
        quarantine = self.database.parent / "runner-recovery-evidence" / f"removed-{fingerprint}"
        self.assertIn(f"mkdir -m 700 {shlex.quote(str(quarantine))}", lines)
        for path in self.pairs[0] + self.pairs[1]:
            self.assertIn(f"mv -n {shlex.quote(str(path))} {shlex.quote(str(quarantine / path.name))}"
                          f" && test ! -e {shlex.quote(str(path))} && test ! -L {shlex.quote(str(path))}", lines)
            self.assertTrue(path.exists())
        self.assertEqual(self.counts()["runner_run"], 0)
        self.assertEqual(len(self.records()), 1)


class TheSignalHandlers(RemoveCase):
    """C-48, C-57, C-66, C-69: real signals against `main` in a child process."""

    def child(self, number, point, times=1, argv=None, fingerprint=None):
        argv = argv or ["repo", "remove", self.source, "--with-items", "--who", "alex", "--reason", "r",
                        "--apply", "--if-fingerprint", fingerprint]
        environment = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(PACKAGE_ROOT)}
        environment.pop("SD_SESSION", None)
        process = subprocess.Popen([sys.executable, "-c", SIGNALLED, str(int(number)), point, str(times),
                                    json.dumps(argv)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=environment)
        try:
            output, errors = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.kill(process.pid, signal.SIGKILL)
            process.wait()
            self.fail(f"the child hung on {point} with signal {number}")
        self.assertEqual(process.returncode, 0, output + errors)
        found = dict(line.split(" ", 1) for line in output.split("\n") if line.startswith(("returned ", "restored ")))
        self.assertEqual(found.get("restored"), "True", output + errors)
        returned = found["returned"]
        return (int(returned) if returned.isdigit() else returned), output + errors

    def fingerprint(self):
        return self.fingerprint_of(self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex",
                                              "--reason", "r").stdout)

    def test_a_signal_before_the_commit_exits_1_and_removes_nothing(self):
        fingerprint = self.fingerprint()
        before = self.counts()
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            for times in (1, 2):
                with self.subTest(signal=number.name, times=times):
                    code, output = self.child(number, "backup", times, fingerprint=fingerprint)
                    self.assertEqual(code, 1, output)
                    self.assertIn("interrupted before the commit; nothing was removed", output)
                    after = self.counts()
                    self.assertEqual({k: v for k, v in after.items() if k != "state"},
                                     {k: v for k, v in before.items() if k != "state"})
                    self.assertGreater(after["state"], before["state"])
                    before = after
        self.assertEqual(self.records(), [])

    def test_a_signal_as_the_commit_returns_exits_0_with_the_move_finished(self):
        fingerprint = self.fingerprint()
        code, output = self.child(signal.SIGTERM, "commit", 2, fingerprint=fingerprint)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.counts()["runner_run"], 0)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(sorted(path.name for path in self.journal.iterdir()), [])
        for path in self.pairs[0] + self.pairs[1]:
            self.assertIn(f"sd-db: moved {path}", output)

    def test_the_old_handlers_come_back_after_an_error_inside_the_transaction(self):
        fingerprint = self.fingerprint()
        code, output = self.child(signal.SIGINT, "ingest", 0, fingerprint=fingerprint)
        self.assertEqual(code, "raised OperationalError: database is locked", output)
        self.assertEqual(self.counts()["runner_run"], 2)


class InterruptAsCommitReturns(RemoveCase):
    """C-57: `main` in this process, with the step 5 wrapper raising `KeyboardInterrupt` after the commit."""

    def test_main_returns_4_and_prints_the_lines(self):
        from contextlib import contextmanager
        from sd_db import removal
        from sd_db.jobs import cli

        fingerprint = self.fingerprint_of(self.sd_db("repo", "remove", self.source, "--with-items", "--who", "alex",
                                                     "--reason", "r").stdout)
        real = removal.transaction

        @contextmanager
        def wrapped(connection):
            with real(connection):
                yield connection
            raise KeyboardInterrupt

        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), mock.patch.object(removal, "transaction", wrapped), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = cli.main(["repo", "remove", self.source, "--with-items", "--who", "alex", "--reason", "r",
                             "--apply", "--if-fingerprint", fingerprint])
        self.assertEqual(code, 4, out.getvalue() + err.getvalue())
        self.assertIn("interrupted", err.getvalue())
        quarantine = self.database.parent / "runner-recovery-evidence" / f"removed-{fingerprint}"
        lines = out.getvalue().split("\n")
        self.assertIn(f"mkdir -m 700 {shlex.quote(str(quarantine))}", lines)
        self.assertEqual([shlex.split(line)[2] for line in lines if line.startswith("mv ")],
                         [str(path) for pair in self.pairs for path in pair])
        self.assertEqual(self.counts()["runner_run"], 0)
        self.assertTrue(all(path.exists() for pair in self.pairs for path in pair))


if __name__ == "__main__":
    unittest.main()
