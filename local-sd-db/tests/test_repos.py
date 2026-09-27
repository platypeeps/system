"""The `repo` table is what the enumeration is bounded by, so it must fill.

Criterion 6 enumerates `docs/work/*/prd.md` across "every repository the
`repo` table holds". Nothing in requirement 2 said how a row gets there, and
an empty table makes that criterion pass over nothing. These are the two ways
it fills and the one path it refuses.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.repos import (
    RepoRefusal,
    add,
    checkouts,
    conf_paths,
    read_conf,
    registered,
    seed,
    set_managed,
    set_runner_merge,
    worktrees_root,
)

from . import support


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
        path = self.conf("# a comment\n\nacme  example-org/aura\nha  a/b\n")
        self.assertEqual(
            read_conf(path), [("acme", "example-org/aura"), ("ha", "a/b")]
        )

    def test_a_line_it_cannot_parse_is_a_refusal_and_not_a_skip(self):
        """A line nobody parses is a repository nobody syncs."""
        path = self.conf("acme example-org/aura\nthis is not a line\n")
        with self.assertRaises(RepoRefusal) as caught:
            read_conf(path)
        self.assertIn("not a line", str(caught.exception))
        self.assertIn(":2:", str(caught.exception))


class Seeding(RepoCase):
    def setUp(self):
        super().setUp()
        (self.checkouts / "acme").mkdir()
        support.repository(self.checkouts / "acme" / "aura")
        self.path = self.conf("acme  example-org/aura\nacme  example-org/absent\n")

    def test_only_checkouts_that_exist_are_registered(self):
        result = seed(
            self.connection, self.path, root=self.checkouts, home=self.home
        )
        self.assertEqual(result.counts, {"registered": 1, "absent": 1})
        self.assertEqual(result.absent, ["example-org/absent"])
        rows = registered(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["path"].endswith("/acme/aura"))

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
            "https://github.com/example-org/aura",
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
        path = self.conf("acme  example-org/aura\nacme  example-org/ssh\n")
        seed(self.connection, path, root=self.checkouts, home=self.home)
        rows = {row["path"]: row["remote"] for row in registered(self.connection)}
        self.assertEqual(
            rows[str(checkout.resolve())], "git@github.com:example-org/ssh.git"
        )



class SeedingTheCommonConf(RepoCase):
    """`repo-sync` reads `repos.common.conf` plus the profile conf on personal.

    The seed read `repos.personal.conf` alone, so a checkout listed only in
    common -- `platypeeps/sd-writing-pack`, `acme/web-supporting-files`
    -- was cloned every night and never registered.
    """

    def setUp(self):
        super().setUp()
        (self.checkouts / "platypeeps").mkdir()
        (self.checkouts / "acme").mkdir()
        support.repository(self.checkouts / "platypeeps" / "shared")
        support.repository(self.checkouts / "acme" / "aura")
        self.personal = self.conf("acme  example-org/aura\n")
        self.common = self.root / "repos.common.conf"
        self.common.write_text(
            "platypeeps  platypeeps/shared\nacme  example-org/aura\n", encoding="utf-8"
        )

    def seed_default(self):
        with mock.patch("sd_db.repos.conf_path", return_value=self.personal):
            return seed(self.connection, None, root=self.checkouts, home=self.home)

    def test_the_default_seed_registers_a_checkout_only_common_names(self):
        result = self.seed_default()
        paths = sorted(Path(row["path"]).name for row in registered(self.connection))
        self.assertEqual(paths, ["aura", "shared"])
        self.assertEqual(result.counts, {"registered": 2, "absent": 0})

    def test_an_entry_in_both_files_is_one_checkout(self):
        with mock.patch("sd_db.repos.conf_path", return_value=self.personal):
            slugs = [checkout.slug for checkout in checkouts(root=self.checkouts)]
        self.assertEqual(slugs, ["platypeeps/shared", "example-org/aura"])

    def test_a_missing_common_conf_leaves_the_personal_one(self):
        self.common.unlink()
        self.assertEqual(self.seed_default().counts, {"registered": 1, "absent": 0})

    def test_a_named_conf_is_read_alone(self):
        """PIN: `repo seed <conf>` means that file, not that file plus common."""
        result = seed(self.connection, self.personal, root=self.checkouts, home=self.home)
        self.assertEqual(result.counts, {"registered": 1, "absent": 0})

    def test_the_shipped_pair_is_what_repo_sync_reads_on_personal(self):
        self.assertEqual(
            [path.name for path in conf_paths()],
            ["repos.common.conf", "repos.personal.conf"],
        )
        # The real pair is gitignored personal config; the repository ships
        # each one as a `.example` template the operator copies into place.
        self.assertTrue(all(path.with_name(path.name + ".example").is_file()
                            for path in conf_paths()))
        for path in conf_paths():
            read_conf(path.with_name(path.name + ".example"))

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
