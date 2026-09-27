"""Regression tests for repo-sync.sh.

The first cases here were defects that actually shipped, each found by a
fixture someone built by hand and then threw away -- three times, for the
same script, inside one evening. The fixtures are the same shape each time: a throwaway
copy of the script, its own conf, a root full of `git init`ed directories with
an origin and no commits. That is cheap enough to keep.

Nothing here touches the network. `reconcile`, `list` and `check` read origins
out of local git config and never fetch. The `nightly` cases either leave
`repo_list` empty by the time sync runs, or hand sync checkouts whose pull
goes through a transport the fixture has stubbed dead (or a local bare
repository). `sync`'s clone path is therefore not covered -- it clones over
SSH, which cannot run in CI -- but every defect this script has had so far
lived in reconcile, list, check, or nightly's exit code.

Each case says which kind it is. REGRESSION means it fails against the code
from before its fix, which is checkable with REPO_SYNC_TEST_SCRIPT and is the
only thing that makes such a test evidence. PIN means it records a decision
that was deliberate and could be undone by accident; a PIN passing against old
code is expected, not a defect in the test.

One case reads the repository rather than a fixture: `ShippedConfTest` opens
the `repos.*.conf.example` files this repo actually ships (and the local
`repos.*.conf` files when present), because what it guards is
those files and not the script.
"""

import ast
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent

# Normally the script next door. The override exists so a regression test can
# be aimed at the code from before its fix -- a test that has never been seen
# to fail has not been shown to test anything:
#
#   REPO_SYNC_TEST_SCRIPT=$(git show 89dcd86:local-repo-sync/repo-sync.sh ...)
SCRIPT = pathlib.Path(os.environ.get("REPO_SYNC_TEST_SCRIPT") or FOLDER / "repo-sync.sh")

NOTIFY_STUB = """#!/bin/sh
# Test stub for local-notify: record the call instead of sending anything.
printf '%s\\n' "$@" >> "$NOTIFY_LOG"
exit 0
"""


AUTOCOMMIT_STUB = """#!/bin/sh
# Test double for local-autocommit: record the argv instead of committing.
printf '%s\\n' "$@" >> "$AUTOCOMMIT_LOG"
exit 0
"""


class Fixture:
    """A disposable copy of repo-sync.sh with its own conf, root and notify.

    The script resolves its conf as `$DIR/repos.$PROFILE.conf` and its notifier
    as `$DIR/../local-notify/notify.sh`, both relative to wherever the script
    itself sits, so the copy has to be laid out the way the repo is. The
    profile has to be one of the three the script allows -- `terra` is the one
    that reads a single conf instead of layering on `repos.common.conf`.
    """

    def __init__(self, script=SCRIPT, profile="terra"):
        self.profile = profile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="repo-sync-test."))
        self.folder = self.tmp / "local-repo-sync"
        self.folder.mkdir()
        shutil.copy(script, self.folder / "repo-sync.sh")
        # Every profile but terra layers the profile conf on the common one,
        # and the commit step has to cover both. Written for every profile so
        # a fixture switched to `personal` needs nothing else.
        (self.folder / "repos.common.conf").write_text("")
        autocommit_dir = self.tmp / "local-autocommit"
        autocommit_dir.mkdir()
        (autocommit_dir / "autocommit.sh").write_text(AUTOCOMMIT_STUB)
        self.autocommit_log = self.tmp / "autocommit.log"
        self.root = self.tmp / "root"
        self.root.mkdir()
        (self.tmp / "home").mkdir()
        notify_dir = self.tmp / "local-notify"
        notify_dir.mkdir()
        (notify_dir / "notify.sh").write_text(NOTIFY_STUB)
        self.notify_log = self.tmp / "notify.log"
        self.write_conf("")

    def destroy(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- building the world -------------------------------------------------

    def write_conf(self, text):
        (self.folder / f"repos.{self.profile}.conf").write_text(text)

    def autocommit_calls(self):
        """Each recorded argv, one list per call, oldest first.

        The double appends one argument per line and nothing else, so a run
        that never reached it leaves no file at all.
        """
        if not self.autocommit_log.exists():
            return []
        lines = self.autocommit_log.read_text().splitlines()
        calls, current = [], []
        for line in lines:
            if line in ("check", "commit") and current:
                calls.append(current)
                current = []
            current.append(line)
        if current:
            calls.append(current)
        return calls

    def autocommit_scopes(self, verb):
        """The scopes one verb was given, in the order it got them."""
        for call in self.autocommit_calls():
            if call and call[0] == verb:
                return [call[i + 1] for i, a in enumerate(call) if a == "--scope"]
        return []

    def checkout(self, rel, origin=None):
        """A directory that scan_disk will see: a git dir, optionally with an
        origin. No commits -- nothing here reads history, and a commit would
        need a user identity CI does not provide."""
        target = self.root / rel
        target.mkdir(parents=True)
        self._git(["init", "-q"], target)
        if origin is not None:
            self._git(["remote", "add", "origin", f"git@github.com:{origin}.git"], target)
        return target

    def local_clone(self, rel):
        """A checkout `sync` can pull without a network: cloned from a bare
        repository next to the root, with one commit so `pull --ff-only` has a
        ref to land on. The identity is passed inline because CI has none.
        Its origin is a path, not github, so scan_disk lists it as unmanaged
        and reconcile leaves both it and its conf entry alone."""
        bare = self.tmp / "bare" / rel
        bare.mkdir(parents=True)
        self._git(["init", "-q", "--bare"], bare)
        seed = self.tmp / "seed" / rel
        seed.mkdir(parents=True)
        self._git(["init", "-q"], seed)
        self._git(["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                   "commit", "-q", "--allow-empty", "-m", "seed"], seed)
        self._git(["push", "-q", str(bare), "HEAD:refs/heads/main"], seed)
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        self._git(["clone", "-q", "-b", "main", str(bare), str(target)], self.tmp)
        return target

    @staticmethod
    def _git(args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    # --- running it ---------------------------------------------------------

    def run(self, *args, expect=0, autocommit=False):
        """Run the script and require `expect` as its status.

        Checking only stdout and the rewritten conf would let a regression
        that prints the right thing and then exits non-zero pass unnoticed.
        Pass expect=None to ignore the status.
        """
        env = dict(os.environ)
        env.update(
            REPO_SYNC_PROFILE=self.profile,
            AUTOCOMMIT_LOG=str(self.autocommit_log),
            REPO_SYNC_ROOT=str(self.root),
            # Must not resolve, or the recorded profile would win over the env.
            MACHINE_SETUP_STATE=str(self.tmp / "no-such-state"),
            HOME=str(self.tmp / "home"),
            NOTIFY_LOG=str(self.notify_log),
            # No invocation may reach the network, and "the fixture gives it
            # nothing to clone" is not enough: aimed at pre-#231 code the
            # nightly case reconciles the mismatch into the conf, and sync
            # then clones it over SSH for real. Failing the transport keeps
            # reconcile and the notification honest while making a clone
            # impossible rather than merely unlikely -- on a runner with no
            # network the alternative is a DNS or connect timeout, not a
            # quick error.
            GIT_SSH_COMMAND="false",
            # The fixture is a throwaway tree with no sibling local-autocommit
            # and no git repository around it, so nightly's commit step has
            # nothing to act on. Off explicitly rather than by accident: the
            # step exits 127 on a real machine when the tool is missing, and a
            # test that relied on that would pass for the wrong reason.
            # local-autocommit has its own suite for the step itself.
            SD_AUTOCOMMIT="0" if not autocommit else "1",
        )
        result = subprocess.run(
            ["sh", str(self.folder / "repo-sync.sh"), *args],
            capture_output=True, text=True, env=env, cwd=str(self.tmp),
        )
        if expect is not None and result.returncode != expect:
            raise AssertionError(
                f"repo-sync.sh {' '.join(args)} exited {result.returncode}, "
                f"wanted {expect}\n--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}"
            )
        return result

    # --- reading it back ----------------------------------------------------

    def entries(self):
        """The conf as repo_list would read it: comments gone, spacing
        normalised, blanks dropped -- but NOT deduplicated, because whether a
        duplicate survives in the file is exactly what some of these assert."""
        out = []
        for line in (self.folder / "repos.terra.conf").read_text().splitlines():
            line = " ".join(line.split("#", 1)[0].split())
            if line:
                out.append(line)
        return out


class RepoSyncTest(unittest.TestCase):
    def fixture(self, **kwargs):
        f = Fixture(**kwargs)
        self.addCleanup(f.destroy)
        return f

    def test_the_commit_step_scopes_every_conf_the_profile_reads(self):
        """REGRESSION: the scope was the profile conf alone.

        `remove_entry` loops over $CONF_FILES, so a removal rewrites
        repos.common.conf too. Committing the profile conf alone left the
        common one dirty -- and `check` defers on a dirty scope, so it stayed
        dirty every night after, which is where a real edit hides.
        """
        f = self.fixture(profile="personal")

        f.run("nightly", autocommit=True)

        want = ["local-repo-sync/repos.common.conf",
                "local-repo-sync/repos.personal.conf"]
        self.assertEqual(sorted(f.autocommit_scopes("check")), want)
        self.assertEqual(sorted(f.autocommit_scopes("commit")), want)

    def test_terra_still_scopes_its_one_conf(self):
        """PIN: terra reads no common conf, and must not claim one."""
        f = self.fixture(profile="terra")

        f.run("nightly", autocommit=True)

        self.assertEqual(f.autocommit_scopes("commit"),
                         ["local-repo-sync/repos.terra.conf"])

    def test_reconcile_run_twice_adds_the_entry_once(self):
        """PIN. The ordinary path: a checkout whose directory is named after its
        repo is added once and then left alone.

        It has to run twice -- one run cannot duplicate anything, so asserting
        a single entry after a single run proves nothing. Note this case does
        NOT reproduce the re-add loop and passes against the code that had it:
        that bug needed a directory whose name differed from its repo, which
        the next test covers. Kept because it pins the behaviour the fix had
        to preserve while removing the loop.
        """
        f = self.fixture()
        f.checkout("a/one", "owner/one")

        first = f.run("reconcile")
        self.assertIn("a owner/one", first.stdout)
        self.assertEqual(f.entries(), ["a owner/one"])

        second = f.run("reconcile")
        self.assertIn("conf matches disk", second.stdout)
        self.assertEqual(f.entries(), ["a owner/one"])

    def test_directory_name_mismatch_is_reported_and_never_added(self):
        """REGRESSION (#231, and the reason the loop existed). A checkout whose directory is not named after its repo cannot be
        written as a conf line at all -- the line would clone to a different
        path than the one on disk. Reporting it is the only correct answer;
        adding it is what looped."""
        f = self.fixture()
        f.checkout("a/fun-ai-stuff", "owner/my-pet-tshirt")

        # This, not the happy path above, is what actually reproduces the
        # loop: the entry reconcile used to append keyed as a/my-pet-tshirt
        # while the checkout keys as a/fun-ai-stuff, so the two could never
        # match and it appended again every night. Three runs, and the conf
        # is compared after each -- a second run appending nothing is the
        # assertion, and it takes more than one run to make it.
        after = []
        for _ in range(3):
            result = f.run("reconcile")
            after.append(f.entries())

        self.assertIn("MISMATCH", result.stdout)
        self.assertIn("fun-ai-stuff", result.stdout)
        self.assertEqual(after, [[], [], []])

    def test_a_repeated_conf_line_is_read_once(self):
        """REGRESSION (#235). `check`, `sync` and `nightly` iterate the list and four totals count
        it, so a duplicate used to mean a repo pulled twice and a total that
        overstated the fleet. Spacing must not defeat the comparison: the real
        confs mix one- and two-space separators."""
        f = self.fixture()
        f.write_conf(
            "a owner/one\n"
            "a owner/two\n"
            "a owner/one\n"
            "a   owner/two\n"
            "a owner/one   # same entry, trailing comment\n"
        )

        result = f.run("list")

        self.assertIn("total   : 2 repos", result.stdout)
        self.assertEqual(result.stdout.count("owner/one"), 1)
        self.assertEqual(result.stdout.count("owner/two"), 1)

    def test_the_list_keeps_conf_order_rather_than_sorting(self):
        """PIN. Deduplicating by sorting would have been shorter and would have
        silently reordered every list, check and sync output out of the
        grouping the confs are written in."""
        f = self.fixture()
        f.write_conf("z owner/zeta\na owner/alpha\na owner/alpha\n")

        result = f.run("list")

        self.assertLess(
            result.stdout.index("owner/zeta"), result.stdout.index("owner/alpha")
        )

    def test_removing_a_gone_checkout_takes_every_copy(self):
        """PIN. remove_entry filters every matching line, not the first -- so a
        duplicate outlives its checkout by exactly zero reconciles."""
        f = self.fixture()
        f.write_conf("a owner/one\na owner/one\n")

        result = f.run("reconcile")

        self.assertIn("removed (checkout gone)", result.stdout)
        self.assertEqual(f.entries(), [])

    def test_a_checkout_without_a_github_origin_is_left_alone(self):
        """PIN. It cannot be recreated from a conf line, so it is reported rather
        than added or deleted."""
        f = self.fixture()
        f.checkout("a/local-only")

        result = f.run("reconcile")

        self.assertIn("unmanaged", result.stdout)
        self.assertIn("local-only", result.stdout)
        self.assertEqual(f.entries(), [])

    def test_nightly_notifies_when_a_checkout_needs_a_rename(self):
        """REGRESSION (#231). Before the mismatch was reported, such a checkout made $ADDED
        non-empty every night and so emailed every night -- wrongly, but the
        operator did hear about it. Fixing the list without adding this would
        have bought silence: a green night nobody reads."""
        f = self.fixture()
        f.checkout("a/fun-ai-stuff", "owner/other-name")

        result = f.run("nightly")

        log = f.notify_log.read_text()
        self.assertIn("need a rename", log)
        self.assertIn("MISMATCH", log)

    def test_nightly_is_silent_when_nothing_drifted(self):
        """PIN. The other half of the same guard: a notification that fires on a
        quiet night trains its reader to ignore it."""
        f = self.fixture()

        f.run("nightly")

        self.assertFalse(
            f.notify_log.exists(),
            f.notify_log.read_text() if f.notify_log.exists() else "",
        )

    def test_nightly_exits_1_when_half_the_fleet_fails(self):
        """REGRESSION (#200). Only a dead email used to fail the job, so four
        straight nights of 41-of-41 sync failures on the work machine exited 0:
        no failures.log entry, no push, and `cron-jobs.sh status` reporting
        "(never exited)". The README went on saying "exits 1 only when an
        email could not be delivered" after this changed (sd:432).

        Reconcile drops a conf entry whose checkout is gone, so the failing
        repos have to exist on disk: a git dir with a github origin that the
        dead transport (GIT_SSH_COMMAND=false) cannot pull from."""
        f = self.fixture()
        f.write_conf("a owner/one\na owner/two\n")
        f.checkout("a/one", "owner/one")
        f.checkout("a/two", "owner/two")

        result = f.run("nightly", expect=1)

        self.assertIn("sync failed 2 of 2 repos", result.stderr)
        self.assertIn("2 repo(s) failed", f.notify_log.read_text())

    def test_nightly_exits_0_when_fewer_than_half_fail(self):
        """PIN. One unreachable repo out of three is a report, not an outage:
        the summary is still mailed, the job does not fail. The healthy two
        pull from a local bare repository, which the dead SSH transport does
        not touch."""
        f = self.fixture()
        f.write_conf("a owner/bad\na owner/good1\na owner/good2\n")
        f.checkout("a/bad", "owner/bad")
        for name in ("good1", "good2"):
            f.local_clone(f"a/{name}")

        result = f.run("nightly", expect=0)

        self.assertIn("failed: 1 repo(s)", result.stdout)
        self.assertNotIn("exiting 1", result.stderr)
        self.assertIn("1 repo(s) failed", f.notify_log.read_text())


class ShippedConfTest(unittest.TestCase):
    """The conf files this repository ships, read from disk rather than built.

    PIN, and the one case here that reads the repo instead of a fixture. It
    closes the third acceptance criterion of the 2026-09-08 item, which asked
    for exactly this shape: no repeated entry for any profile, `asserted by
    enumerating the conf files rather than by grepping for this repository's
    name`. Enumerating is the point -- a check built from filenames typed into
    the test cannot notice a conf nobody remembered to add.

    It guards a decision that made a mistake quiet. Before #235 a repeated
    line was loud in the wrong way: the repo was cloned and pulled twice every
    run and four totals overstated the fleet. After #235 `repo_list` emits each
    entry once, so the repeat is inert -- and invisible. The file still holds
    the mistake and nothing else will ever say so. Reconcile will not take it
    out either, unless the checkout itself disappears.

    Expected to pass against old code, like any PIN: `repo_list` changed, the
    confs did not.
    """

    COMMON = "repos.common.conf"
    EXAMPLE = ".example"

    @staticmethod
    def entries(conf):
        """The lines `repo_list` would read out of one conf file.

        The same normalisation the script applies before comparing: strip
        comments, collapse runs of whitespace to one space, drop what is left
        empty. `a owner/one` and `a   owner/one` are the same entry.
        """
        found = []
        for raw in conf.read_text().splitlines():
            line = " ".join(raw.split("#", 1)[0].split())
            if line:
                found.append(line)
        return found

    def confs(self):
        """The shipped `.example` confs, plus the local gitignored confs when
        this checkout has them -- both are what some profile reads."""
        found = sorted(FOLDER.glob("repos.*.conf.example"))
        self.assertTrue(found, f"no repos.*.conf.example beside {FOLDER}")
        return found + sorted(FOLDER.glob("repos.*.conf"))

    @staticmethod
    def repeated(entries):
        seen, twice = set(), []
        for entry in entries:
            if entry in seen and entry not in twice:
                twice.append(entry)
            seen.add(entry)
        return twice

    def test_no_conf_file_repeats_an_entry(self):
        """PIN. Within one file. A hand edit that repeats a line is inert now,
        which is why something has to say it is there."""
        for conf in self.confs():
            with self.subTest(conf=conf.name):
                twice = self.repeated(self.entries(conf))
                self.assertEqual([], twice, f"{conf.name} repeats: {twice}")

    def test_no_profile_reads_an_entry_twice(self):
        """PIN. Across the pair a profile actually reads. Every profile is
        common + profile, except terra, which reads its own conf alone because
        a terra machine carries only this toolbox."""
        for conf in self.confs():
            profile = conf.name.split(".")[1]
            if profile == "common":
                continue
            with self.subTest(profile=profile):
                files = [conf]
                if profile != "terra":
                    suffix = self.EXAMPLE if conf.name.endswith(self.EXAMPLE) else ""
                    files.insert(0, FOLDER / (self.COMMON + suffix))
                entries = [e for f in files for e in self.entries(f)]
                twice = self.repeated(entries)
                names = " + ".join(f.name for f in files)
                self.assertEqual([], twice, f"{names} repeats: {twice}")

    def test_the_readme_names_every_group_common_carries(self):
        """PIN. The README's profile table is the one place that says what
        common holds; a group added to the file and not to the row makes the
        table describe less than a personal or work machine syncs."""
        rows = [line for line in (FOLDER / "README.md").read_text().splitlines()
                if line.startswith("| `%s`" % self.COMMON)]
        self.assertEqual(1, len(rows), "README has no single common row")
        shipped = FOLDER / (self.COMMON + self.EXAMPLE)
        groups = dict.fromkeys(e.split()[0] for e in self.entries(shipped))
        missing = [g for g in groups if "`%s" % g not in rows[0]]
        self.assertEqual([], missing, f"README common row omits: {missing}")


class SuiteKindTest(unittest.TestCase):
    """PIN. Every case names its kind, because the README tells a reader to
    trust a REGRESSION and not to expect a PIN to fail against old code; a
    case that names neither makes that sentence false for it."""

    def test_every_case_opens_with_its_kind(self):
        tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
        unnamed = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            fallback = ast.get_docstring(node) or ""
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name.startswith("test"):
                    doc = ast.get_docstring(member) or fallback
                    if not doc.startswith(("REGRESSION", "PIN")):
                        unnamed.append(f"{node.name}.{member.name}")
        self.assertEqual([], unnamed, "cases that name no kind")


if __name__ == "__main__":
    unittest.main()
