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
import sys
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"
sys.path.insert(0, str(LIB))
import system_tools_config  # noqa: E402

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


def attention(result):
    """The SD_REPORT_ATTENTION reasons a run printed for its report (sd:3454)."""
    return [line.split(": ", 1)[1] for line in result.stdout.splitlines()
            if line.startswith("SD_REPORT_ATTENTION: ")]


SD_STUB = """#!/bin/sh
# Test stub for sd: hygiene reads the bulk storage root from SD_BULK_STORAGE_ROOT,
# so no case reads the machine's own config or sweeps its lane storage.
case "$*" in
  "config get sd.bulk_storage_root")
    [ -n "${SD_BULK_STORAGE_ROOT:-}" ] || { echo "sd: sd.bulk_storage_root is not set" >&2; exit 1; }
    echo "$SD_BULK_STORAGE_ROOT" ;;
  *) echo "sd stub: unexpected $*" >&2; exit 2 ;;
esac
"""


SD_DB_STUB = """#!/bin/sh
# Test stub for the pack's bin/sd-db (SD_PACK_ROOT): a workflow database in
# files under $SD_DB_STUB, so no case reads the machine's own. It logs each
# call to $SD_DB_STUB/calls.
#   clones  the `repo list --clones` answer. Without it the stub answers with
#           the fixture's confs, as a store seeded from them does.
#   rows    one registered path per line, for `repo list`; REPO_LIST names a
#           file to print instead, and REPO_LIST_RC fails the verb.
# CLONES_RC fails `repo list --clones`; ADD_RC and CLONES_SET_RC fail
# `repo add` and `repo clones`.
printf '%s\\n' "$*" >> "$SD_DB_STUB/calls"
answer() {
  if [ -e "$SD_DB_STUB/clones" ]; then cat "$SD_DB_STUB/clones"; return; fi
  dir=${REPO_SYNC_CONF_DIR:-$SYSTEM_TOOLS_CONFIG/repo-sync}
  if [ "$1" = terra ]; then set -- "$dir/repos.terra.conf"
  else set -- "$dir/repos.common.conf" "$dir/repos.$1.conf"; fi
  cat "$@" 2>/dev/null | sed 's/#.*//' | awk 'NF == 2 && !seen[$1 " " $2]++ { print $1 " " $2 }'
}
case "$1 $2" in
  "repo list")
    if [ "${3:-}" = --clones ]; then
      [ -z "${CLONES_RC:-}" ] || { echo "sd-db: the hub refused this build" >&2; exit "$CLONES_RC"; }
      answer "$4"
      exit 0
    fi
    [ -z "${REPO_LIST_RC:-}" ] || { echo "sd-db: boom" >&2; exit "$REPO_LIST_RC"; }
    if [ -n "${REPO_LIST:-}" ]; then cat "$REPO_LIST"; exit 0; fi
    [ ! -e "$SD_DB_STUB/rows" ] || sed 's/^/sd-db: /; s/$/  -  no  github  hub  manual/' "$SD_DB_STUB/rows"
    ;;
  "repo add")
    [ -z "${ADD_RC:-}" ] || { echo "sd-db: database is locked" >&2; exit "$ADD_RC"; }
    (cd "$3" && pwd -P) >> "$SD_DB_STUB/rows"
    echo "sd-db: registered $3"
    ;;
  "repo clones")
    [ -z "${CLONES_SET_RC:-}" ] || { echo "sd-db: database is locked" >&2; exit "$CLONES_SET_RC"; }
    path=$(cd "$3" && pwd -P)
    rel=${path#"$(cd "$REPO_SYNC_ROOT" && pwd -P)"/}
    case "$rel" in */*) sub=${rel%/*} ;; *) sub=. ;; esac
    own=$("${REAL_GIT:-git}" -C "$path" remote get-url origin)
    own=${own#git@github.com:}
    profile=${4#+}
    answer "$profile" > "$SD_DB_STUB/clones.new"
    echo "$sub ${own%.git}" >> "$SD_DB_STUB/clones.new"
    mv "$SD_DB_STUB/clones.new" "$SD_DB_STUB/clones"
    echo "sd-db: $path clone_profiles none -> $profile"
    ;;
  *) echo "sd-db stub: unexpected $*" >&2; exit 2 ;;
esac
"""


GIT_WRAPPER = """#!/bin/sh
# Test wrapper for git: record the call, then run the real git, so the script
# still scans and pulls while the test sees every verb it used.
printf '%s\\n' "$*" >> "$GIT_LOG"
exec "$REAL_GIT" "$@"
"""


class Fixture:
    """A disposable copy of repo-sync.sh with its own conf, root and notify.

    The script resolves its conf as `$REPO_SYNC_CONF_DIR/repos.$PROFILE.conf`
    (the fixture aims that at the copy's own folder) and its notifier
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
        (self.tmp / "lib").mkdir()
        shutil.copy(LIB / "config.sh", self.tmp / "lib" / "config.sh")
        shutil.copy(LIB / "bounded.sh", self.tmp / "lib" / "bounded.sh")
        # Every profile but terra layers the profile conf on the common one.
        # Written for every profile so a fixture switched to `personal` needs
        # nothing else.
        (self.folder / "repos.common.conf").write_text("")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "git").write_text(GIT_WRAPPER)
        (self.bin / "git").chmod(0o755)
        (self.bin / "sd").write_text(SD_STUB)
        (self.bin / "sd").chmod(0o755)
        self.git_log = self.tmp / "git.log"
        # The pack's sd-db is a stub holding its rows in files (SD_DB_STUB).
        self.sd_pack = self.tmp / "sd-pack-stub"
        (self.sd_pack / "bin").mkdir(parents=True)
        (self.sd_pack / "bin" / "sd-db").write_text(SD_DB_STUB)
        (self.sd_pack / "bin" / "sd-db").chmod(0o755)
        self.db = self.tmp / "db"
        self.db.mkdir()
        self.state_home = self.tmp / "state"
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

    def git_verbs(self):
        """Every git call the script made, as one argument string each."""
        if not self.git_log.exists():
            return []
        return self.git_log.read_text().splitlines()

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

    def run(self, *args, expect=0, extra_env=None):
        """Run the script and require `expect` as its status.

        Checking only stdout and the rewritten conf would let a regression
        that prints the right thing and then exits non-zero pass unnoticed.
        Pass expect=None to ignore the status.
        """
        env = dict(os.environ)
        env.update(
            REPO_SYNC_PROFILE=self.profile,
            GIT_LOG=str(self.git_log),
            REAL_GIT=shutil.which("git"),
            PATH=f"{self.bin}{os.pathsep}{env['PATH']}",
            REPO_SYNC_ROOT=str(self.root),
            # The confs sit in the copy's folder, so a case can write them
            # without a config directory.
            REPO_SYNC_CONF_DIR=str(self.folder),
            # Must not resolve, or the recorded profile would win over the env.
            MACHINE_SETUP_STATE=str(self.tmp / "no-such-state"),
            HOME=str(self.tmp / "home"),
            # The list's database read and its snapshot are the fixture's.
            SD_PACK_ROOT=str(self.sd_pack),
            SD_DB_STUB=str(self.db),
            XDG_STATE_HOME=str(self.state_home),
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
            # The nightly SSH preflight (sd:2160) must not load keys into the
            # real agent of the machine running the suite.
            REPO_SYNC_SSH_ADD="true",
        )
        for key, value in (extra_env or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
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

    def clones(self, text):
        """What `sd-db repo list --clones` answers from now on, in place of
        the confs."""
        (self.db / "clones").write_text(text)

    def rows(self, *rels):
        """Register checkouts `rels` under the root, as `repo add` does."""
        with (self.db / "rows").open("a") as rows:
            for rel in rels:
                rows.write(f"{(self.root / rel).resolve()}\n")

    def db_calls(self, verb=""):
        """Every sd-db call the script made that starts with `verb`."""
        calls = self.db / "calls"
        lines = calls.read_text().splitlines() if calls.exists() else []
        return [line for line in lines if line.startswith(verb)]

    def snapshot(self):
        return self.state_home / "system" / "repo-sync" / f"clones.{self.profile}"

    def quiet_fleet(self):
        """One listed checkout that pulls from a local bare repository, so
        the list is a database answer and a nightly has nothing to report."""
        self.local_clone("a/good")
        self.write_conf("a owner/good\n")

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

    def test_nightly_leaves_the_confs_and_commits_nothing(self):
        """REGRESSION (sd:3983). The list lives in the database now: nightly
        writes no conf, even for a listed checkout that is gone, and no git
        call stages, commits or pushes anything."""
        f = self.fixture(profile="personal")
        (f.folder / "repos.common.conf").write_text("a owner/gone-common\n")
        f.write_conf("a owner/gone-profile\n")
        f.checkout("a/kept", "owner/kept")
        f.rows("a/kept")

        f.run("nightly", expect=None)

        self.assertEqual((f.folder / "repos.common.conf").read_text(), "a owner/gone-common\n")
        self.assertEqual((f.folder / "repos.personal.conf").read_text(), "a owner/gone-profile\n")
        self.assertTrue(f.git_verbs(), "the wrapper saw no git call")
        for call in f.git_verbs():
            words = call.split()
            for verb in ("add", "commit", "push"):
                self.assertNotIn(verb, words, call)

    def test_reconcile_joins_a_checkout_with_no_row_once(self):
        """REGRESSION (sd:3983). A GitHub checkout on disk with no row is
        registered and listed for the profile, through `repo add` then
        `repo clones PATH +PROFILE`, and the conf is not touched. Run twice:
        the second run finds the row and the line, and writes nothing."""
        f = self.fixture()
        f.checkout("a/one", "owner/one")
        f.checkout("a/two", "owner/two")
        f.rows("a/one")
        f.clones("a owner/one\n")
        two = str(f.root / "a" / "two")

        first = f.run("reconcile")
        self.assertIn("joined", first.stdout)
        self.assertIn("a owner/two", first.stdout)
        self.assertEqual(f.db_calls("repo add"), [f"repo add {two}"])
        self.assertEqual(f.db_calls("repo clones"), [f"repo clones {two} +terra"])
        self.assertEqual(f.entries(), [])

        second = f.run("reconcile")
        self.assertIn("list matches disk", second.stdout)
        self.assertEqual(len(f.db_calls("repo add")), 1)
        self.assertEqual(len(f.db_calls("repo clones")), 1)

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

    def test_a_gone_listed_checkout_is_reported_and_never_removed(self):
        """REGRESSION (sd:3983). Deleting a checkout no longer stops its sync:
        no run removes a profile, so no outage or empty disk can look like a
        deletion. Reconcile names the line, says sync clones it, and names
        the one way to stop it."""
        f = self.fixture()
        f.checkout("a/kept", "owner/kept")
        f.rows("a/kept", "a/one")
        f.clones("a owner/kept\na owner/one\n")

        result = f.run("reconcile")

        self.assertIn("listed for terra, no checkout: sync clones it", result.stdout)
        self.assertIn("a owner/one", result.stdout)
        self.assertIn("repo clones PATH -terra", result.stdout)
        self.assertEqual(f.db_calls("repo clones"), [])
        self.assertEqual((f.db / "clones").read_text(), "a owner/kept\na owner/one\n")
        self.assertNotIn("remove_entry", SCRIPT.read_text())

    def test_a_checkout_without_a_github_origin_is_left_alone(self):
        """PIN. It cannot be recreated from a conf line, so it is reported rather
        than added or deleted."""
        f = self.fixture()
        f.checkout("a/local-only")

        result = f.run("reconcile")

        self.assertIn("unmanaged", result.stdout)
        self.assertIn("local-only", result.stdout)
        self.assertEqual(f.entries(), [])

    def test_nightly_needs_attention_when_a_checkout_needs_a_rename(self):
        """REGRESSION (#231). Before the mismatch was reported, such a checkout made $ADDED
        non-empty every night and so emailed every night -- wrongly, but the
        operator did hear about it. Fixing the list without adding this would
        have bought silence: a green night nobody reads. sd:3454: the run's
        report carries it to Reports and Today, and nothing is mailed."""
        f = self.fixture()
        f.quiet_fleet()
        f.checkout("a/fun-ai-stuff", "owner/other-name")

        result = f.run("nightly")

        self.assertIn("need a rename", " ".join(attention(result)))
        self.assertIn("MISMATCH", result.stdout)
        self.assertFalse(f.notify_log.exists(), f.notify_log.read_text() if f.notify_log.exists() else "")

    def test_nightly_is_silent_when_nothing_drifted(self):
        """PIN. The other half of the same guard: a report that needs
        attention on a quiet night trains its reader to ignore it."""
        f = self.fixture()
        f.quiet_fleet()

        result = f.run("nightly")

        self.assertEqual(attention(result), [])
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
        self.assertIn("2 repo(s) failed", " ".join(attention(result)))

    def test_nightly_exits_0_when_fewer_than_half_fail(self):
        """PIN. One unreachable repo out of three is a report, not an outage:
        the report needs attention, the job does not fail. The healthy two
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
        [reason] = attention(result)
        self.assertRegex(reason, r"^repo-sync: 1 repo\(s\) failed on ")
        self.assertFalse(f.notify_log.exists(), f.notify_log.read_text() if f.notify_log.exists() else "")


    def test_each_day_files_its_own_report(self):
        """REGRESSION (sd:3537). The ingest folds a reason it holds open and
        keeps the first body. A checksum with digits erased made worker1 and
        worker2 failing one report."""
        reasons = []
        for bad, day in (("worker1", "2026-10-12"), ("worker2", "2026-10-13")):
            f = self.fixture()
            (f.bin / "date").write_text('#!/bin/sh\n[ "$1" = "+%Y-%m-%d" ] && { echo "$FAKE_DAY"; exit 0; }\n'
                                        'exec /bin/date "$@"\n')
            (f.bin / "date").chmod(0o755)
            f.write_conf(f"a owner/{bad}\na owner/good1\na owner/good2\n")
            f.checkout(f"a/{bad}", f"owner/{bad}")
            for name in ("good1", "good2"):
                f.local_clone(f"a/{name}")
            reasons.append(attention(f.run("nightly", expect=0, extra_env={"FAKE_DAY": day})))
        self.assertRegex(" ".join(reasons[0]), r"^repo-sync: 1 repo\(s\) failed on \S+, 2026-10-12$")
        self.assertRegex(" ".join(reasons[1]), r", 2026-10-13$")

    def test_nightly_names_a_locked_ssh_key_as_the_cause(self):
        """REGRESSION (sd:2160). On 2026-09-29, 63 of 65 failures were "Permission
        denied (publickey)": a reboot left the key locked. The report listed 65
        repos and never said why. It now names the key first, after one try to
        load it from the keychain."""
        f = self.fixture()
        f.write_conf("a owner/one\na owner/two\n")
        f.checkout("a/one", "owner/one")
        f.checkout("a/two", "owner/two")
        ssh_add_log = f.tmp / "ssh-add.log"
        stub = f.bin / "ssh-add-stub"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{ssh_add_log}"\n')
        stub.chmod(0o755)

        result = f.run("nightly", expect=1, extra_env={"REPO_SYNC_SSH_ADD": str(stub)})

        self.assertEqual(ssh_add_log.read_text(), "--apple-load-keychain\n")
        self.assertIn("no key authenticates to git@github.com", result.stdout)
        self.assertLess(result.stdout.index("mac-utils.sh addkey"), result.stdout.index("failed repos:"))

    def test_nightly_loads_keychain_keys_before_the_sweep(self):
        """PIN (sd:2160). A key the keychain can unlock is loaded, the run says
        so, and nothing is reported: the night is healthy."""
        f = self.fixture()
        marker = f.tmp / "key-loaded"
        ssh = f.bin / "ssh-stub"
        ssh.write_text(f'#!/bin/sh\n[ -e "{marker}" ] || exit 255\n'
                       'echo "Hi owner! You\'ve successfully authenticated, but GitHub does not provide shell access."\n'
                       "exit 1\n")
        ssh.chmod(0o755)
        ssh_add = f.bin / "ssh-add-stub"
        ssh_add.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        ssh_add.chmod(0o755)

        f.quiet_fleet()

        result = f.run("nightly", extra_env={"GIT_SSH_COMMAND": str(ssh), "REPO_SYNC_SSH_ADD": str(ssh_add)})

        self.assertIn("loaded key(s) from the keychain", result.stdout)
        self.assertNotIn("no key authenticates", result.stdout)
        self.assertEqual(attention(result), [])

class DatabaseListTest(unittest.TestCase):
    """sd:3983: the list is `sd-db repo list --clones PROFILE`, read once per
    run; else the last answer's snapshot; else the confs; else a refusal."""

    def fixture(self, **kwargs):
        f = Fixture(**kwargs)
        self.addCleanup(f.destroy)
        return f

    def test_the_list_comes_from_the_database(self):
        """REGRESSION. The database answers, so the conf is not read."""
        f = self.fixture()
        f.write_conf("a owner/from-conf\n")
        f.clones("a owner/from-db\n")

        result = f.run("list")

        self.assertIn("owner/from-db", result.stdout)
        self.assertNotIn("owner/from-conf", result.stdout)
        self.assertIn("source  : database", result.stdout)
        self.assertEqual(f.db_calls(), ["repo list --clones terra"])
        self.assertEqual("", result.stderr)

    def test_an_answer_replaces_the_snapshot_through_a_temp_file(self):
        """REGRESSION. The answer goes to a temp file, then `mv`. A temp file
        a killed run left is overwritten, not read."""
        f = self.fixture()
        f.clones("a owner/one\n. owner/two\n")
        f.snapshot().parent.mkdir(parents=True)
        f.snapshot().write_text("a owner/old\n")
        left = f.snapshot().with_name(f.snapshot().name + ".tmp")
        left.write_text("half a lin")

        f.run("list")

        self.assertEqual("a owner/one\n. owner/two\n", f.snapshot().read_text())
        self.assertFalse(left.exists())

    def test_a_failed_read_falls_back_to_the_snapshot(self):
        """REGRESSION. A failed read is unknown, never an empty list: the
        last answer stands, and stderr names the failure and the source."""
        f = self.fixture()
        f.write_conf("a owner/from-conf\n")
        f.snapshot().parent.mkdir(parents=True)
        f.snapshot().write_text("a owner/from-snapshot\n")

        result = f.run("list", extra_env={"CLONES_RC": "1"})

        self.assertIn("owner/from-snapshot", result.stdout)
        self.assertNotIn("owner/from-conf", result.stdout)
        self.assertIn("source  : snapshot", result.stdout)
        self.assertIn("the hub refused this build", result.stderr)
        self.assertIn(str(f.snapshot()), result.stderr)
        self.assertEqual("a owner/from-snapshot\n", f.snapshot().read_text())

    def test_an_empty_answer_is_unknown_and_keeps_the_snapshot(self):
        """REGRESSION. Every profile lists at least `system`, so no line is a
        profile never imported, not an empty fleet: the snapshot is kept, it
        answers, and nightly's report names the fallback."""
        f = self.fixture()
        f.local_clone("a/good")
        f.clones("")
        f.snapshot().parent.mkdir(parents=True)
        f.snapshot().write_text("a owner/good\n")

        result = f.run("nightly")

        self.assertEqual("a owner/good\n", f.snapshot().read_text())
        self.assertIn("lists nothing for terra", result.stderr)
        [reason] = attention(result)
        self.assertRegex(reason, r"^repo-sync: the database did not answer; the list came from the snapshot on ")
        self.assertIn("pull --ff-only", " ".join(f.git_verbs()))

    def test_a_line_that_is_not_a_conf_line_is_unknown(self):
        """REGRESSION. An answer is used whole or not at all: one line that is
        not `<subdir> <owner/repo>` makes it unknown, and the snapshot stays."""
        f = self.fixture()
        f.clones("a owner/one\nsd-db: ~/repos/a/two  git@github.com:owner/two.git  no\n")
        f.snapshot().parent.mkdir(parents=True)
        f.snapshot().write_text("a owner/kept\n")

        result = f.run("list")

        self.assertIn("owner/kept", result.stdout)
        self.assertNotIn("owner/one", result.stdout)
        self.assertIn("not a <subdir> <owner/repo> line", result.stderr)
        self.assertEqual("a owner/kept\n", f.snapshot().read_text())

    def test_no_answer_and_no_snapshot_reads_the_confs(self):
        """REGRESSION. The fresh-machine bootstrap: a machine that never
        reached the hub syncs from the conf it was given, and says so."""
        f = self.fixture()
        f.write_conf("a owner/from-conf\n")

        result = f.run("list", extra_env={"CLONES_RC": "1"})

        self.assertIn("owner/from-conf", result.stdout)
        self.assertIn("source  : conf", result.stdout)
        self.assertIn("no snapshot", result.stderr)
        self.assertFalse(f.snapshot().exists())

    def test_nothing_answers_refuses_naming_both_remedies(self):
        """REGRESSION. No database answer, no snapshot and no conf: refuse,
        and name both ways out."""
        f = self.fixture()
        (f.folder / "repos.terra.conf").unlink()

        result = f.run("list", expect=1, extra_env={"CLONES_RC": "1"})

        self.assertIn("sd-db repo seed --clones terra", result.stderr)
        self.assertIn("copy local-repo-sync/repos.terra.conf.example to", result.stderr)
        self.assertIn("the hub refused this build", result.stderr)

    def test_nightly_reads_the_list_once(self):
        """REGRESSION. One read per run: sync, hygiene and the total read the
        run's copy. A join reads it once more, after reconcile, so sync sees
        the joined checkout."""
        f = self.fixture()
        f.quiet_fleet()

        f.run("nightly")
        self.assertEqual(f.db_calls("repo list --clones"), ["repo list --clones terra"])

        f.checkout("b/new", "owner/new")
        (f.db / "calls").unlink()
        result = f.run("nightly", expect=None)
        self.assertEqual(len(f.db_calls("repo list --clones")), 2)
        self.assertIn("=== refreshing owner/new", result.stdout)

    def test_a_hub_that_does_not_answer_makes_reconcile_write_nothing(self):
        """REGRESSION. From the snapshot or the conf, reconcile cannot know
        which checkout has a row, so it writes nothing; sync still runs, and
        the report names the source."""
        f = self.fixture()
        f.quiet_fleet()
        f.checkout("b/new", "owner/new")

        result = f.run("nightly", extra_env={"CLONES_RC": "1"})

        self.assertEqual(f.db_calls("repo add") + f.db_calls("repo clones"), [])
        self.assertIn("present, not listed for terra", result.stdout)
        self.assertIn("reconcile wrote nothing", result.stdout)
        self.assertIn("pull --ff-only", " ".join(f.git_verbs()))
        [reason] = attention(result)
        self.assertRegex(reason, r"^repo-sync: the database did not answer; the list came from the conf on ")

    def test_a_join_stopped_after_add_is_reported_not_finished(self):
        """REGRESSION. Stopped between `repo add` and `repo clones +P`, the
        next run finds a row without P. A cleared P may be the operator's
        choice, so it reports the checkout and writes nothing."""
        f = self.fixture()
        f.quiet_fleet()
        f.checkout("b/new", "owner/new")

        first = f.run("reconcile", extra_env={"CLONES_SET_RC": "1"})
        self.assertIn("not joined", first.stdout)
        self.assertIn("database is locked", first.stdout)
        self.assertEqual(len(f.db_calls("repo add")), 1)

        second = f.run("reconcile")
        self.assertIn("present, not listed for terra", second.stdout)
        self.assertIn("b owner/new", second.stdout)
        self.assertEqual(len(f.db_calls("repo add")), 1)
        self.assertEqual(len(f.db_calls("repo clones")), 1)

    def test_a_failed_add_writes_nothing_more(self):
        """REGRESSION. When `repo add` fails, `repo clones` is not called; the
        checkout is named, and nightly reports it before anything else."""
        f = self.fixture()
        f.quiet_fleet()
        f.checkout("b/new", "owner/new")

        result = f.run("nightly", expect=None, extra_env={"ADD_RC": "1"})

        self.assertEqual(f.db_calls("repo clones"), [])
        self.assertIn("b owner/new (sd-db: database is locked)", result.stdout)
        [reason] = attention(result)
        self.assertRegex(reason, r"^repo-sync: 1 checkout\(s\) not joined to the list on ")

    def test_a_missing_pack_is_a_failed_read(self):
        """PIN. No sd-db to run is the database not answering, not an empty
        list."""
        f = self.fixture()
        f.write_conf("a owner/from-conf\n")

        result = f.run("list", extra_env={"SD_PACK_ROOT": str(f.tmp / "no-pack")})

        self.assertIn("owner/from-conf", result.stdout)
        self.assertIn("exited 127", result.stderr)

    def test_an_unreadable_conf_is_no_list(self):
        """REGRESSION (codex pass). With no database answer and no snapshot,
        a conf that cannot be read refuses; it is not an empty fleet."""
        f = self.fixture()
        conf = f.folder / "repos.terra.conf"
        conf.write_text("a owner/one\n")
        conf.chmod(0)
        self.addCleanup(conf.chmod, 0o644)

        result = f.run("list", expect=1, extra_env={"CLONES_RC": "1"})

        self.assertIn(f"the config {conf} is missing or unreadable", result.stderr)
        self.assertNotIn("total", result.stdout)

    def test_a_failed_row_read_needs_attention(self):
        """REGRESSION (codex pass). The list answered but `repo list` did not:
        reconcile wrote nothing, and nightly's report says so."""
        f = self.fixture()
        f.quiet_fleet()

        result = f.run("nightly", extra_env={"REPO_LIST_RC": "1"})

        self.assertIn("reconcile wrote nothing: sd-db repo list failed: sd-db: boom", result.stdout)
        [reason] = attention(result)
        self.assertRegex(reason, r"^repo-sync: reconcile could not read the repo rows on ")

    def test_a_home_relative_row_is_a_row(self):
        """PIN. `repo list` prints a path under $HOME as `~/...`, as the
        store keys it; that row counts, so its checkout is not joined again."""
        f = self.fixture()
        f.quiet_fleet()
        f.checkout("b/new", "owner/new")
        (f.db / "rows").write_text("~/root/b/new\n")

        result = f.run("reconcile", extra_env={"HOME": str(f.tmp)})

        self.assertIn("present, not listed for terra", result.stdout)
        self.assertEqual(f.db_calls("repo add"), [])

    def test_reconcile_on_a_machine_with_no_root_yet_reports_every_line(self):
        """REGRESSION. A fresh machine has no checkout root until sync makes
        it; reconcile still runs and names every listed line for sync."""
        f = self.fixture()
        f.clones("a owner/one\n")
        shutil.rmtree(f.root)

        result = f.run("reconcile")

        self.assertIn("listed for terra, no checkout: sync clones it", result.stdout)
        self.assertIn("a owner/one", result.stdout)

    def test_another_profiles_snapshot_is_not_read(self):
        """PIN. The snapshot's name carries its profile: a machine whose
        profile changed reads its own, or the confs, never the old one."""
        f = self.fixture()
        f.write_conf("a owner/from-conf\n")
        other = f.snapshot().with_name("clones.personal")
        other.parent.mkdir(parents=True)
        other.write_text("a owner/personal-only\n")

        result = f.run("list", extra_env={"CLONES_RC": "1"})

        self.assertIn("owner/from-conf", result.stdout)
        self.assertNotIn("owner/personal-only", result.stdout)

    def test_a_snapshot_restores_the_previous_script(self):
        """REGRESSION. The README's revert: with the confs deleted, the
        snapshot copied to the profile conf and an empty common conf are a
        list the conf reader reads whole."""
        f = self.fixture(profile="personal")
        f.clones("a owner/one\n. owner/two\n")
        f.run("list")
        (f.folder / "repos.common.conf").write_text("")
        shutil.copy(f.snapshot(), f.folder / "repos.personal.conf")
        f.snapshot().unlink()

        result = f.run("list", extra_env={"CLONES_RC": "1"})

        self.assertIn("source  : conf", result.stdout)
        self.assertIn("owner/one", result.stdout)
        self.assertIn("owner/two", result.stdout)
        self.assertIn("total   : 2 repos", result.stdout)


class ConfigDirTest(unittest.TestCase):
    def fixture(self, **kwargs):
        f = Fixture(**kwargs)
        self.addCleanup(f.destroy)
        return f

    def test_the_confs_default_to_the_shared_config_directory(self):
        """PIN. With no REPO_SYNC_CONF_DIR the confs come from
        $SYSTEM_TOOLS_CONFIG/repo-sync, not from beside the script, and
        nightly leaves both as they were (sd:3983)."""
        f = self.fixture(profile="terra")
        conf_root = f.tmp / "config"
        (conf_root / "repo-sync").mkdir(parents=True)
        (conf_root / "repo-sync" / "repos.terra.conf").write_text(
            "only owner/from-config\n")
        f.write_conf("beside owner/from-folder\n")

        env = {"REPO_SYNC_CONF_DIR": None, "SYSTEM_TOOLS_CONFIG": str(conf_root)}
        listed = f.run("list", extra_env=env)
        self.assertIn("owner/from-config", listed.stdout)
        self.assertNotIn("owner/from-folder", listed.stdout)

        f.run("nightly", expect=None, extra_env=env)
        self.assertEqual("only owner/from-config\n",
                         (conf_root / "repo-sync" / "repos.terra.conf").read_text())
        self.assertEqual("beside owner/from-folder\n", (f.folder / "repos.terra.conf").read_text())

    def test_a_missing_conf_names_the_config_path_and_the_example(self):
        """PIN. The error says where the conf belongs and what to copy."""
        f = self.fixture(profile="terra")
        conf_root = f.tmp / "config"
        env = {"REPO_SYNC_CONF_DIR": None, "SYSTEM_TOOLS_CONFIG": str(conf_root)}
        result = f.run("list", expect=1, extra_env=env)
        want = conf_root / "repo-sync" / "repos.terra.conf"
        self.assertIn(f"copy local-repo-sync/repos.terra.conf.example to {want}",
                      result.stderr)

    def test_the_shared_work_root_selects_the_work_profile(self):
        """PIN. SYSTEM_TOOLS_WORK_ROOT stands in for REPO_SYNC_WORK_ROOT, the
        same meaning local-ai-apps reads from AI_APPS_WORK_ROOT."""
        f = self.fixture(profile="work")
        (f.folder / "repos.work.conf").write_text("only owner/work-one\n")
        env = {"REPO_SYNC_PROFILE": None, "REPO_SYNC_WORK_ROOT": None,
               "SYSTEM_TOOLS_WORK_ROOT": str(f.root)}
        listed = f.run("list", extra_env=env)
        self.assertIn("owner/work-one", listed.stdout)


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
        """The shipped `.example` confs, plus this machine's live confs in
        <config>/repo-sync when it has them -- both are what some profile
        reads."""
        found = sorted(FOLDER.glob("repos.*.conf.example"))
        self.assertTrue(found, f"no repos.*.conf.example beside {FOLDER}")
        live = system_tools_config.config_dir("repo-sync")
        return found + sorted(live.glob("repos.*.conf"))

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
                    files.insert(0, conf.parent / (self.COMMON + suffix))
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
