"""The `repo` table is what the enumeration is bounded by, so it must fill.

Criterion 6 enumerates `docs/work/*/prd.md` across "every repository the
`repo` table holds". Nothing in requirement 2 said how a row gets there, and
an empty table makes that criterion pass over nothing. These are the two ways
it fills and the one path it refuses.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.repos import (
    CI_MODES,
    SATELLITE_GATE_VALUES,
    ConfMissing,
    RepoRefusal,
    add,
    checkouts,
    conf_paths,
    read_conf,
    registered,
    repo_ci,
    repo_satellite_gate,
    seed,
    set_ci,
    set_lane_host,
    set_managed,
    set_runner_merge,
    set_satellite_gate,
    worktrees_root,
)

from . import support

#: The repo-sync folder of this checkout, which ships the `.example` lists.
REPO_SYNC = Path(__file__).resolve().parents[2] / "local-repo-sync"


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkouts = self.root / "repos"
        self.checkouts.mkdir()
        database = self.root / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)

    def conf(self, text: str) -> Path:
        path = self.root / "repos.personal.conf"
        path.write_text(text, encoding="utf-8")
        return path


class ReadingTheConf(RepoCase):
    def test_comments_and_blanks_are_skipped(self):
        path = self.conf("# a comment\n\nacme  example-org/anvil\nha  a/b\n")
        self.assertEqual(
            read_conf(path), [("acme", "example-org/anvil"), ("ha", "a/b")]
        )

    def test_a_line_it_cannot_parse_is_a_refusal_and_not_a_skip(self):
        """A line nobody parses is a repository nobody syncs."""
        path = self.conf("acme example-org/anvil\nthis is not a line\n")
        with self.assertRaises(RepoRefusal) as caught:
            read_conf(path)
        self.assertIn("not a line", str(caught.exception))
        self.assertIn(":2:", str(caught.exception))


class Seeding(RepoCase):
    def setUp(self):
        super().setUp()
        (self.checkouts / "acme").mkdir()
        support.repository(self.checkouts / "acme" / "anvil")
        self.path = self.conf("acme  example-org/anvil\nacme  example-org/absent\n")

    def test_only_checkouts_that_exist_are_registered(self):
        result = seed(
            self.connection, self.path, root=self.checkouts, home=self.home
        )
        self.assertEqual(result.counts, {"registered": 1, "absent": 1})
        self.assertEqual(result.absent, ["example-org/absent"])
        rows = registered(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["path"].endswith("/acme/anvil"))

    def test_a_second_seed_registers_the_same_set(self):
        first = seed(self.connection, self.path, root=self.checkouts, home=self.home)
        second = seed(self.connection, self.path, root=self.checkouts, home=self.home)
        self.assertEqual(first.registered, second.registered)
        self.assertEqual(len(registered(self.connection)), 1)

    def test_the_remote_is_recorded(self):
        """With no origin on the checkout, the conf's rendering is the answer."""
        seed(self.connection, self.path, root=self.checkouts, home=self.home)
        self.assertEqual(
            registered(self.connection)[0]["remote"],
            "https://github.com/example-org/anvil",
        )

    def test_a_checkouts_own_origin_outranks_the_confs_rendering(self):
        """The conf names a slug, so `Checkout.remote` can only ever render https.

        A machine that clones over ssh carries an ssh origin. Before sd:1436
        `same_remote` did not treat the two spellings as one repository, so
        letting the conf win left every seeded row unable to resolve its own
        checkout by origin. The row still records what the checkout says.
        """
        checkout = self.checkouts / "acme" / "ssh"
        support.repository(checkout, bare=self.root / "ssh.git")
        support.git(checkout, "remote", "set-url", "origin",
                    "git@github.com:example-org/ssh.git")
        path = self.conf("acme  example-org/anvil\nacme  example-org/ssh\n")
        seed(self.connection, path, root=self.checkouts, home=self.home)
        rows = {row["path"]: row["remote"] for row in registered(self.connection)}
        self.assertEqual(
            rows[str(checkout.resolve())], "git@github.com:example-org/ssh.git"
        )



class SeedingTheCommonConf(RepoCase):
    """`repo-sync` reads `repos.common.conf` plus the profile conf on personal.

    The seed read `repos.personal.conf` alone, so a checkout listed only in
    common -- `platypeeps/writing-pack`, `acme/web-supporting-files`
    -- was cloned every night and never registered.
    """

    def setUp(self):
        super().setUp()
        (self.checkouts / "platypeeps").mkdir()
        (self.checkouts / "acme").mkdir()
        support.repository(self.checkouts / "platypeeps" / "shared")
        support.repository(self.checkouts / "acme" / "anvil")
        self.personal = self.conf("acme  example-org/anvil\n")
        self.common = self.root / "repos.common.conf"
        self.common.write_text(
            "platypeeps  platypeeps/shared\nacme  example-org/anvil\n", encoding="utf-8"
        )

    def seed_default(self):
        with mock.patch("sd_db.repos.conf_path", return_value=self.personal):
            return seed(self.connection, None, root=self.checkouts, home=self.home)

    def test_the_default_seed_registers_a_checkout_only_common_names(self):
        result = self.seed_default()
        paths = sorted(Path(row["path"]).name for row in registered(self.connection))
        self.assertEqual(paths, ["anvil", "shared"])
        self.assertEqual(result.counts, {"registered": 2, "absent": 0})

    def test_an_entry_in_both_files_is_one_checkout(self):
        with mock.patch("sd_db.repos.conf_path", return_value=self.personal):
            slugs = [checkout.slug for checkout in checkouts(root=self.checkouts)]
        self.assertEqual(slugs, ["platypeeps/shared", "example-org/anvil"])

    def test_a_missing_common_conf_leaves_the_personal_one(self):
        self.common.unlink()
        self.assertEqual(self.seed_default().counts, {"registered": 1, "absent": 0})

    def test_a_named_conf_is_read_alone(self):
        """PIN: `repo seed <conf>` means that file, not that file plus common."""
        result = seed(self.connection, self.personal, root=self.checkouts, home=self.home)
        self.assertEqual(result.counts, {"registered": 1, "absent": 0})

    def test_the_shipped_pair_is_what_repo_sync_reads_on_personal(self):
        with mock.patch.dict(os.environ, {"SYSTEM_TOOLS_CONFIG": str(self.root / "cfg")}):
            os.environ.pop("REPO_SYNC_PROFILE", None)
            self.assertEqual(
                [path.name for path in conf_paths()],
                ["repos.common.conf", "repos.personal.conf"],
            )
        # The real pair is private config; the repository ships each one as a
        # `.example` template in local-repo-sync the operator copies into place.
        shipped = conf_paths(root=REPO_SYNC)
        self.assertTrue(all(path.with_name(path.name + ".example").is_file()
                            for path in shipped))
        for path in shipped:
            read_conf(path.with_name(path.name + ".example"))


class TheConfigDirectory(RepoCase):
    """repo-sync's lists live in `<config>/repo-sync/`, not in the checkout."""

    def setUp(self):
        super().setUp()
        self.config = self.root / "cfg"
        self.lists = self.config / "repo-sync"
        self.lists.mkdir(parents=True)
        support.repository(self.checkouts / "acme" / "anvil")
        support.repository(self.checkouts / "platypeeps" / "shared")
        patcher = mock.patch.dict(os.environ, {"SYSTEM_TOOLS_CONFIG": str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("REPO_SYNC_PROFILE", None)

    def test_the_default_pair_is_read_from_the_config_directory(self):
        self.assertEqual(conf_paths(), [self.lists / "repos.common.conf",
                                        self.lists / "repos.personal.conf"])
        (self.lists / "repos.common.conf").write_text("platypeeps  platypeeps/shared\n")
        (self.lists / "repos.personal.conf").write_text("acme  example-org/anvil\n")
        result = seed(self.connection, None, root=self.checkouts, home=self.home)
        self.assertEqual(result.counts, {"registered": 2, "absent": 0})

    def test_the_profile_names_the_list(self):
        os.environ["REPO_SYNC_PROFILE"] = "work"
        self.assertEqual(conf_paths()[-1], self.lists / "repos.work.conf")
        os.environ["REPO_SYNC_PROFILE"] = "terra"
        self.assertEqual(conf_paths(), [self.lists / "repos.terra.conf"])

    def test_an_absent_profile_list_fails_naming_where_it_goes(self):
        with self.assertRaises(ConfMissing) as caught:
            seed(self.connection, None, root=self.checkouts, home=self.home)
        message = str(caught.exception)
        self.assertIn(str(self.lists / "repos.personal.conf"), message)
        self.assertIn("local-repo-sync/repos.personal.conf.example", message)
        self.assertIsInstance(caught.exception, FileNotFoundError)
        self.assertEqual(registered(self.connection), [])

    def test_the_command_reports_the_absent_list_and_exits_1(self):
        import contextlib
        import io

        from sd_db.jobs import cli
        err = io.StringIO()
        with mock.patch.object(cli, "_open_for_write", lambda: connect(self.root / "sd.db")), \
                mock.patch.object(cli, "_home", lambda: self.home), \
                contextlib.redirect_stderr(err):
            rc = cli.command_repo(["seed"])
        self.assertEqual(rc, 1)
        self.assertIn("repos.personal.conf is not set", err.getvalue())
        self.assertIn(str(self.lists / "repos.personal.conf"), err.getvalue())


class Adding(RepoCase):
    def test_a_checkout_is_registered_with_the_mode_read_from_it(self):
        checkout = support.repository(self.checkouts / "one")
        add(self.connection, checkout, home=self.home)
        row = registered(self.connection)[0]
        self.assertEqual(row["mode"], "work")

    def test_a_directory_that_is_not_a_checkout_is_refused(self):
        plain = self.checkouts / "plain"
        plain.mkdir()
        with self.assertRaises(RepoRefusal) as caught:
            add(self.connection, plain, home=self.home)
        self.assertIn("not a git checkout", str(caught.exception))

    def test_a_clone_under_the_worktrees_directory_is_refused(self):
        """A runner's clone is not a registered repository.

        Criterion 6's own test clones a registered repository under the
        worktrees directory and asserts the enumeration does not widen. It
        does not widen because the table is the bound; this refusal is what
        stops an operator putting one in the table by hand.
        """
        worktrees = worktrees_root(self.home)
        worktrees.mkdir(parents=True)
        clone = support.repository(worktrees / "item-clone")
        with self.assertRaises(RepoRefusal) as caught:
            add(self.connection, clone, home=self.home)
        self.assertIn("worktrees directory", str(caught.exception))
        self.assertEqual(registered(self.connection), [])


class TheManagedWriter(RepoCase):
    """sd:1619. `repo.managed` says the operator manages the repository.

    The writer mirrors `set_runner_merge`: it writes, it reports what it
    replaced, it refuses a value that is not a flag word, and it refuses a
    path no row holds rather than making one.
    """

    def _registered(self) -> str:
        checkout = support.repository(self.checkouts / "one")
        return add(self.connection, checkout, home=self.home)

    def _value(self, path: str) -> int:
        return self.connection.execute(
            "SELECT managed FROM repo WHERE path = ?", (path,)).fetchone()[0]

    def test_it_sets_the_column_and_names_what_it_replaced(self):
        path = self._registered()
        self.assertEqual(self._value(path), 0)
        self.assertEqual(set_managed(self.connection, path, "yes"), (path, "no"))
        self.assertEqual(self._value(path), 1)
        self.assertEqual(set_managed(self.connection, path, "no"), (path, "yes"))
        self.assertEqual(self._value(path), 0)

    def test_it_changes_no_other_field_on_the_row(self):
        path = self._registered()
        before = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        set_managed(self.connection, path, "yes")
        after = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        moved = {key for key in before if before[key] != after[key]}
        self.assertEqual(moved - {"updated_at"}, {"managed"})

    def test_a_value_that_is_not_a_flag_word_is_refused_in_a_sentence(self):
        path = self._registered()
        for value in ("true", "1", "Yes", ""):
            with self.subTest(value=value), self.assertRaises(RepoRefusal) as caught:
                set_managed(self.connection, path, value)
            self.assertIn("yes or no", str(caught.exception))
        self.assertEqual(self._value(path), 0)

    def test_an_unregistered_path_is_refused_rather_than_registered(self):
        checkout = support.repository(self.checkouts / "two")
        with self.assertRaises(RepoRefusal) as caught:
            set_managed(self.connection, checkout, "yes")
        self.assertIn("not a registered repository", str(caught.exception))
        self.assertEqual(registered(self.connection), [])


if __name__ == "__main__":
    unittest.main()


class TheRunnerMergeWriter(RepoCase):
    """sd:1131. `repo.runner_merge` had four readers and no caller that set it.

    Every registered repository therefore kept the schema default forever, and
    the only way to change one was to import the library and call
    `upsert_repo` by hand. These are the writer's four behaviours: it writes,
    it reports what it replaced, it refuses a value the CHECK would reject,
    and it refuses a path no row holds rather than making one.
    """

    def _registered(self) -> str:
        checkout = support.repository(self.checkouts / "one")
        return add(self.connection, checkout, home=self.home)

    def _value(self, path: str) -> str:
        return self.connection.execute(
            "SELECT runner_merge FROM repo WHERE path = ?", (path,)).fetchone()[0]

    def test_it_sets_the_column_and_names_what_it_replaced(self):
        path = self._registered()
        self.assertEqual(self._value(path), "manual")
        self.assertEqual(set_runner_merge(self.connection, path, "auto"), (path, "manual"))
        self.assertEqual(self._value(path), "auto")
        self.assertEqual(set_runner_merge(self.connection, path, "manual"), (path, "auto"))
        self.assertEqual(self._value(path), "manual")

    def test_it_changes_no_other_field_on_the_row(self):
        path = self._registered()
        before = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        set_runner_merge(self.connection, path, "auto")
        after = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        moved = {key for key in before if before[key] != after[key]}
        self.assertEqual(moved - {"updated_at"}, {"runner_merge"})

    def test_a_value_the_check_would_reject_is_refused_in_a_sentence(self):
        path = self._registered()
        with self.assertRaises(RepoRefusal) as caught:
            set_runner_merge(self.connection, path, "automatic")
        self.assertIn("manual or auto", str(caught.exception))
        self.assertEqual(self._value(path), "manual")

    def test_an_unregistered_path_is_refused_rather_than_registered(self):
        checkout = support.repository(self.checkouts / "two")
        with self.assertRaises(RepoRefusal) as caught:
            set_runner_merge(self.connection, checkout, "auto")
        self.assertIn("not a registered repository", str(caught.exception))
        self.assertEqual(registered(self.connection), [])


class TheCiWriter(RepoCase):
    """sd:1843. `repo.ci` says whether a repository's checks run in GitHub
    Actions or as a local `sd-check`.

    The writer mirrors `set_runner_merge`; the reader answers `github` for a
    path the table does not hold, so a caller never treats an unregistered
    repository as switched to local checks.
    """

    def _registered(self) -> str:
        checkout = support.repository(self.checkouts / "one")
        return add(self.connection, checkout, home=self.home)

    def test_the_modes_are_the_two_the_check_lists_default_first(self):
        self.assertEqual(CI_MODES, ("github", "local"))

    def test_it_sets_the_column_names_what_it_replaced_and_reads_back(self):
        path = self._registered()
        self.assertEqual(repo_ci(self.connection, path), "github")
        self.assertEqual(set_ci(self.connection, path, "local"), (path, "github"))
        self.assertEqual(repo_ci(self.connection, path), "local")
        self.assertEqual(set_ci(self.connection, path, "github"), (path, "local"))
        self.assertEqual(repo_ci(self.connection, path), "github")

    def test_the_reader_resolves_an_absolute_checkout_path_to_its_row(self):
        checkout = support.repository(self.checkouts / "one")
        path = add(self.connection, checkout, home=self.home)
        set_ci(self.connection, path, "local")
        self.assertEqual(repo_ci(self.connection, checkout), "local")

    def test_it_changes_no_other_field_on_the_row(self):
        path = self._registered()
        before = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        set_ci(self.connection, path, "local")
        after = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        moved = {key for key in before if before[key] != after[key]}
        self.assertEqual(moved - {"updated_at"}, {"ci"})

    def test_a_value_the_check_would_reject_is_refused_in_a_sentence(self):
        path = self._registered()
        for value in ("actions", "GitHub", ""):
            with self.subTest(value=value), self.assertRaises(RepoRefusal) as caught:
                set_ci(self.connection, path, value)
            self.assertIn("github or local", str(caught.exception))
        self.assertEqual(repo_ci(self.connection, path), "github")

    def test_an_unregistered_path_is_refused_rather_than_registered(self):
        checkout = support.repository(self.checkouts / "two")
        with self.assertRaises(RepoRefusal) as caught:
            set_ci(self.connection, checkout, "local")
        self.assertIn("not a registered repository", str(caught.exception))
        self.assertEqual(registered(self.connection), [])

    def test_the_reader_answers_github_for_an_unregistered_path(self):
        checkout = support.repository(self.checkouts / "two")
        self.assertEqual(repo_ci(self.connection, checkout), "github")
        self.assertEqual(registered(self.connection), [])


class TheSatelliteGateWriter(RepoCase):
    """sd:2704, ruling Q1. `repo.satellite_gate` is the operator's grant for
    the hub to merge on a satellite's gate pass.

    The writer mirrors `set_ci`; the reader answers `off` for a row never set
    and for a path the table does not hold, so nothing reads a grant the
    operator did not give.
    """

    def _registered(self) -> str:
        checkout = support.repository(self.checkouts / "one")
        return add(self.connection, checkout, home=self.home)

    def test_the_values_are_the_two_the_check_lists_default_first(self):
        self.assertEqual(SATELLITE_GATE_VALUES, ("off", "accept"))

    def test_an_unset_row_reads_off_and_a_set_row_reads_accept(self):
        path = self._registered()
        self.assertEqual(repo_satellite_gate(self.connection, path), "off")
        self.assertEqual(set_satellite_gate(self.connection, path, "accept"), (path, "off"))
        self.assertEqual(repo_satellite_gate(self.connection, path), "accept")
        self.assertEqual(set_satellite_gate(self.connection, path, "off"), (path, "accept"))
        self.assertEqual(repo_satellite_gate(self.connection, path), "off")

    def test_the_reader_resolves_an_absolute_checkout_path_and_an_unregistered_one(self):
        checkout = support.repository(self.checkouts / "one")
        path = add(self.connection, checkout, home=self.home)
        set_satellite_gate(self.connection, path, "accept")
        self.assertEqual(repo_satellite_gate(self.connection, checkout), "accept")
        absent = support.repository(self.checkouts / "two")
        self.assertEqual(repo_satellite_gate(self.connection, absent), "off")

    def test_it_changes_no_other_field_on_the_row(self):
        path = self._registered()
        before = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        set_satellite_gate(self.connection, path, "accept")
        after = dict(self.connection.execute(
            "SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        moved = {key for key in before if before[key] != after[key]}
        self.assertEqual(moved - {"updated_at"}, {"satellite_gate"})

    def test_a_bad_value_and_an_unregistered_path_are_refused(self):
        path = self._registered()
        for value in ("on", "Accept", ""):
            with self.subTest(value=value), self.assertRaises(RepoRefusal) as caught:
                set_satellite_gate(self.connection, path, value)
            self.assertIn("off or accept", str(caught.exception))
        self.assertEqual(repo_satellite_gate(self.connection, path), "off")
        checkout = support.repository(self.checkouts / "two")
        with self.assertRaises(RepoRefusal) as caught:
            set_satellite_gate(self.connection, checkout, "accept")
        self.assertIn("not a registered repository", str(caught.exception))



class TheLaneHostWriter(RepoCase):
    """sd:3075. `set_lane_host` is the one writer of `repo.lane_host`: a
    host name, or `hub`, stored as NULL. It moves no other field."""

    def _registered(self) -> str:
        checkout = support.repository(self.checkouts / "one")
        return add(self.connection, checkout, home=self.home)

    def _stored(self, path: str):
        return self.connection.execute("SELECT lane_host FROM repo WHERE path = ?", (path,)).fetchone()[0]

    def test_a_host_is_stored_and_hub_stores_null_each_answering_the_value_before(self):
        path = self._registered()
        self.assertIsNone(self._stored(path))
        self.assertEqual(set_lane_host(self.connection, path, "build-2"), (path, "hub"))
        self.assertEqual(self._stored(path), "build-2")
        self.assertEqual(set_lane_host(self.connection, path, "hub"), (path, "build-2"))
        self.assertIsNone(self._stored(path))

    def test_it_changes_no_other_field_on_the_row(self):
        path = self._registered()
        before = dict(self.connection.execute("SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        set_lane_host(self.connection, path, "build-2")
        after = dict(self.connection.execute("SELECT * FROM repo WHERE path = ?", (path,)).fetchone())
        self.assertEqual({key for key in before if before[key] != after[key]} - {"updated_at"}, {"lane_host"})

    def test_a_bad_name_and_an_unregistered_path_are_refused(self):
        path = self._registered()
        for value in ("Build_2", "build.example.test", "", "build 2"):
            with self.subTest(value=value), self.assertRaises(RepoRefusal) as caught:
                set_lane_host(self.connection, path, value)
            self.assertIn("not a lane host", str(caught.exception))
        self.assertIsNone(self._stored(path))
        with self.assertRaises(RepoRefusal) as caught:
            set_lane_host(self.connection, support.repository(self.checkouts / "two"), "build-2")
        self.assertIn("not a registered repository", str(caught.exception))

    def _clones(self, *remotes: str) -> list[str]:
        made = []
        for index, remote in enumerate(remotes):
            path = add(self.connection, support.repository(self.checkouts / f"clone-{index}"), home=self.home)
            self.connection.execute("UPDATE repo SET remote = ? WHERE path = ?", (remote, path))
            made.append(path)
        return made

    def test_every_clone_of_the_remote_moves_and_no_other_repository(self):
        # Review finding 2 (sd:3075): the lane belongs to the remote, so clones never disagree.
        first, second, other = self._clones("git@github.com:example/one.git",
                                            "https://github.com/Example/One", "git@github.com:example/two.git")
        self.connection.execute("UPDATE repo SET lane_host = 'build-3' WHERE path = ?", (second,))
        self.assertEqual(set_lane_host(self.connection, first, "build-2"), (first, "hub"))
        self.assertEqual([self._stored(path) for path in (first, second, other)], ["build-2", "build-2", None])
        set_lane_host(self.connection, second, "hub")
        self.assertEqual([self._stored(path) for path in (first, second, other)], [None, None, None])

    def test_a_queued_or_running_merge_on_any_clone_refuses_the_move(self):
        # Review finding 1 (sd:3075): a live merge's ship holds the old host's lock.
        first, second = self._clones("git@github.com:example/one.git", "git@github.com:example/one.git")
        self.connection.execute(
            "INSERT INTO item (id, kind, title, status, repo, created_at, updated_at) "
            "VALUES (41, 'task', 'merge me', 'in_progress', ?, '2026-10-08', '2026-10-08')", (second,))
        for status in ("queued", "running", "ending"):
            self.connection.execute("DELETE FROM assignment")
            self.connection.execute("INSERT INTO assignment (id, item, role, status) VALUES (7, 41, 'merge', ?)",
                                    (status,))
            with self.subTest(status=status), self.assertRaises(RepoRefusal) as caught:
                set_lane_host(self.connection, first, "build-2")
            self.assertIn("assignment 7 (sd:41)", str(caught.exception))
            self.assertEqual([self._stored(first), self._stored(second)], [None, None])
        self.connection.execute("UPDATE assignment SET status = 'done'")
        self.connection.execute("INSERT INTO assignment (id, item, role, status) VALUES (8, 41, 'author', 'running')")
        self.assertEqual(set_lane_host(self.connection, first, "build-2"), (first, "hub"))
