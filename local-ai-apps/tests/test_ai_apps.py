"""Regression tests for ai-apps.sh nightly.

The nightly runs unattended and reports by email only when something moved.
That makes the quiet night the dangerous one: there is no email, so stderr
and the exit status are the only channels left. A failure that returns 0 on a
quiet night is invisible until somebody notices the inventory went stale.

The fixture is a throwaway tree laid out the way the repository is, because
the script resolves its siblings as "$DIR/..". `brew` and `notify.sh` are
doubles, and a `git` double records any call: nothing upgrades or sends, and
the nightly must not touch git at all.

Each case says which kind it is. REGRESSION means it fails against a script
without the guard it names. PIN means it records a deliberate decision.
"""

import os
import hashlib
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = os.environ.get("AI_APPS_TEST_SCRIPT", str(FOLDER / "ai-apps.sh"))

BREW_STUB = """#!/bin/sh
# Nothing is ever outdated, so the nightly has no upgrade to report.
exit 0
"""

NOTIFY_STUB = """#!/bin/sh
printf '%s\\n' "$@" >> "$NOTIFY_LOG"
exit 0
"""

# The format line every capture writes. A manifest that carries it holds
# encoded labels; one without it holds literal labels (sd:1826).
FORMAT = "# format: app|kind|name[|key]  (names only — safe to commit)\n"
# The format line before labels were encoded (sd:1273).
OLD_FORMAT = "# format: app|kind|name  (names only — safe to commit)\n"

# opencode is outdated, and its upgrade never finishes: a brew stalled on a
# permission check nobody can answer, as on the nights of sd:2660.
BREW_HANG_STUB = """#!/bin/sh
case "$1" in
  outdated) echo opencode ;;
  upgrade) : > "$BREW_STARTED"; exec sleep 60 ;;
esac
"""

# The query itself never finishes, so nothing is checked or upgraded.
BREW_OUTDATED_HANG_STUB = """#!/bin/sh
case "$1" in
  outdated) exec sleep 60 ;;
esac
"""

# opencode is outdated, and its upgrade fails at once.
BREW_UPGRADE_FAILS_STUB = """#!/bin/sh
case "$1" in
  outdated) echo opencode ;;
  upgrade) echo "Error: opencode: download failed" >&2; exit 1 ;;
esac
"""

STEP_LINE = r"\[step \d\d:\d\d:\d\d\] brew upgrade opencode \(bound {}s\)"

GIT_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$GIT_LOG"
exit 0
"""


# codex and claude-code are outdated; the weekly upgrade owns them (sd:3033).
BREW_WEEKLY_ONLY_STUB = """#!/bin/sh
case "$1" in
  outdated) echo codex; echo claude-code ;;
  upgrade) echo "upgrade $*" >> "$BREW_LOG" ;;
esac
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-apps-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.folder = self.tmp / "local-ai-apps"
        self.folder.mkdir()
        self.script = self.folder / "ai-apps.sh"
        shutil.copy(SCRIPT, self.script)
        (self.tmp / "lib").mkdir()
        for library in (FOLDER.parent / "lib").glob("*.sh"):
            shutil.copy(library, self.tmp / "lib" / library.name)
        self.script.chmod(0o755)
        (self.folder / "profiles").mkdir()

        self.home = self.tmp / "home"
        self.home.mkdir()
        self.notify_log = self.tmp / "notify.log"
        self.git_log = self.tmp / "git.log"
        self._double(self.tmp / "local-notify" / "notify.sh", NOTIFY_STUB)

        self.bin = self.tmp / "bin"
        self._double(self.bin / "brew", BREW_STUB)
        self._double(self.bin / "git", GIT_STUB)

    @staticmethod
    def _double(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def run_tool(self, *args, extra_env=None):
        env = dict(os.environ)
        env.update(
            AI_APPS_PROFILE="personal",
            # The manifests sit in the copy's own folder, as in a private
            # fork that tracks them.
            AI_APPS_PROFILES_DIR=str(self.folder / "profiles"),
            # Must not resolve, or the recorded profile would beat the env.
            MACHINE_SETUP_STATE=str(self.tmp / "no-such-state"),
            HOME=str(self.home),
            NOTIFY_LOG=str(self.notify_log),
            GIT_LOG=str(self.git_log),
            PATH=f"{self.bin}:{env['PATH']}",
        )
        for key, value in (extra_env or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(
            ["sh", str(self.script), *args],
            capture_output=True, text=True, env=env, cwd=str(self.tmp),
        )

    def opencode_config(self, text):
        path = self.home / ".config" / "opencode" / "opencode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def inventory(self):
        return (self.folder / "profiles" / "personal.inv").read_text()

    def quiet_night(self):
        """A night with no upgrade and no inventory change.

        The first run writes the inventory, so it is a changed night by
        definition. The second finds it identical and is the quiet one.
        """
        first = self.run_tool("nightly")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.notify_log.unlink(missing_ok=True)
        return self.run_tool("nightly")


class QuietNightTest(Fixture):
    def test_a_quiet_night_exits_0_without_email(self):
        """PIN: the ordinary quiet night stays silent and green."""
        result = self.quiet_night()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.notify_log.exists(),
                         self.notify_log.read_text()
                         if self.notify_log.exists() else "")

    def test_a_changed_night_emails_the_report(self):
        """PIN: the first capture is a change, and a change is mailed."""
        result = self.run_tool("nightly")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ai-apps: changes on", self.notify_log.read_text())

    def test_the_nightly_never_touches_git(self):
        """PIN: the inventory keeps no git history.

        The nightly writes its manifest and stops; nothing stages, commits
        or pushes it, on a changed night or a quiet one.
        """
        self.quiet_night()

        self.assertFalse(self.git_log.exists(),
                         self.git_log.read_text() if self.git_log.exists() else "")


class HungStepTest(Fixture):
    """A brew step that hangs names itself and ends (sd:2660).

    The nightly captures the update's output for its report, so a hang used
    to leave nothing in the job log until the job's limit killed it.
    """

    def setUp(self):
        super().setUp()
        self._double(self.bin / "brew", BREW_HANG_STUB)
        self.started = self.tmp / "brew-started"

    def test_a_hung_upgrade_is_logged_and_stopped_at_its_bound(self):
        """REGRESSION: the step is bounded and named in the log. The night
        then fails (FailedUpdateTest), and still mails its report."""
        result = self.run_tool("nightly", extra_env={
            "AI_APPS_STEP_TIMEOUT": "2", "BREW_STARTED": str(self.started)})

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stderr, STEP_LINE.format(2))
        self.assertIn("timed out after 2s: brew upgrade opencode", result.stdout)
        self.assertIn("timed out after 2s: brew upgrade opencode", self.notify_log.read_text())

    def test_a_term_mid_step_exits_without_writing_into_the_removed_temp_dir(self):
        """REGRESSION: the job's limit ends the run; it does not carry on.

        The TERM trap removed the temporary folder and returned, so the run
        went on to write the capture into it: `cap: No such file or
        directory` in the log of the night the limit fired.
        """
        env = dict(os.environ)
        env.update(AI_APPS_PROFILE="personal", AI_APPS_PROFILES_DIR=str(self.folder / "profiles"),
                   MACHINE_SETUP_STATE=str(self.tmp / "no-such-state"), HOME=str(self.home),
                   NOTIFY_LOG=str(self.notify_log), GIT_LOG=str(self.git_log),
                   BREW_STARTED=str(self.started), PATH=f"{self.bin}:{env['PATH']}")
        # A group of its own, as cron-jobs' run_bounded gives a job: its limit
        # sends TERM to the whole group.
        process = subprocess.Popen(["sh", str(self.script), "nightly"], env=env,
                                   cwd=str(self.tmp), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self._reap, process)
        deadline = time.monotonic() + 20
        while not self.started.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(self.started.exists(), "brew upgrade never started")

        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)

        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertNotIn("No such file or directory", stdout + stderr)
        self.assertRegex(stderr, STEP_LINE.format(1800))

    @staticmethod
    def _reap(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


class FailedUpdateTest(Fixture):
    """A failed or timed-out update step fails the night (sd:2662).

    The update's code used to be dropped with `|| true`. A brew query that
    timed out on a quiet night then exited 0 with no mail: the step line in
    the job log was the only trace. A nonzero exit is the nightly's failure
    channel, as for a failed capture: cron-jobs raises its failure banner
    and push.
    """

    def test_a_timed_out_query_fails_a_quiet_night(self):
        """REGRESSION: the quiet night is the one nothing else reports."""
        first = self.run_tool("nightly")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.notify_log.unlink(missing_ok=True)
        self._double(self.bin / "brew", BREW_OUTDATED_HANG_STUB)

        result = self.run_tool("nightly", extra_env={
            "AI_APPS_STEP_TIMEOUT": "1", "ST_BOUNDED_GRACE": "1"})

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ai-apps: the app update failed (exit 1)", result.stderr)
        self.assertIn("timed out after 1s: brew outdated --quiet", result.stdout)
        self.assertFalse(self.notify_log.exists(), "an unchanged inventory mailed")

    def test_a_failed_upgrade_mails_the_report_and_fails(self):
        """REGRESSION: the report still goes, and the night still fails."""
        self._double(self.bin / "brew", BREW_UPGRADE_FAILS_STUB)

        result = self.run_tool("nightly")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ai-apps: the app update failed (exit 1)", result.stderr)
        self.assertIn("Error: opencode: download failed", self.notify_log.read_text())


class ConfigDirTest(Fixture):
    def test_capture_writes_into_the_shared_config_dir(self):
        """PIN. With no AI_APPS_PROFILES_DIR the manifest goes to
        $SYSTEM_TOOLS_CONFIG/ai-apps/profiles, not beside the script, and
        nightly writes it there too."""
        config = self.tmp / "config"
        env = {"AI_APPS_PROFILES_DIR": None, "SYSTEM_TOOLS_CONFIG": str(config)}
        result = self.run_tool("capture", extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((config / "ai-apps" / "profiles" / "personal.inv").is_file())
        self.assertFalse((self.folder / "profiles" / "personal.inv").exists())
        (config / "ai-apps" / "profiles" / "personal.inv").unlink()
        nightly = self.run_tool("nightly", extra_env=env)
        self.assertEqual(nightly.returncode, 0, nightly.stdout + nightly.stderr)
        self.assertTrue((config / "ai-apps" / "profiles" / "personal.inv").is_file())
        self.assertFalse((self.folder / "profiles" / "personal.inv").exists())

    def test_the_shared_work_root_selects_the_work_profile(self):
        """PIN. SYSTEM_TOOLS_WORK_ROOT stands in for AI_APPS_WORK_ROOT, the
        same meaning local-repo-sync reads from REPO_SYNC_WORK_ROOT."""
        env = {"AI_APPS_PROFILE": None, "AI_APPS_WORK_ROOT": None,
               "SYSTEM_TOOLS_WORK_ROOT": str(self.tmp)}
        result = self.run_tool("capture", extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.folder / "profiles" / "work.inv").is_file())


class ConfigReadingTest(Fixture):
    """What the capture does with a config it cannot parse.

    Every app here keeps JSON that the app itself also accepts with `//` line
    comments. The capture read one of them strictly, and a comment in that one
    file emptied every row it contributes.
    """

    def test_line_comments_do_not_empty_an_app(self):
        """REGRESSION: opencode.json was the one config read strictly.

        A `//` note beside a model option made the file unparseable to this
        reader, `jload` swallowed the error and answered {}, and the
        2026-09-21 capture recorded opencode as having no MCP servers and no
        plugins -- a diff shaped exactly like an uninstall.
        """
        self.opencode_config(
            '{\n'
            '  // the servers this machine reaches\n'
            '  "mcp": {"github": {}},\n'
            '  "plugin": ["./plugins/claude-mem.js"]\n'
            '}\n'
        )
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("opencode|mcp|github", self.inventory())
        self.assertIn("opencode|plugin|claude-mem", self.inventory())

    def test_an_unparseable_config_leaves_the_inventory_alone(self):
        """REGRESSION: absent and unparseable used to share one answer, {}.

        Absent is the ordinary state of an app this machine does not run.
        Unparseable means every row that file contributes is missing, and
        writing that absence records an uninstall that never happened.
        """
        self.opencode_config('{"mcp": {"github": {}}}\n')
        self.assertEqual(self.run_tool("capture").returncode, 0)
        before = self.inventory()

        # Truncated, not merely JSONC: a trailing comma is a shape the app
        # accepts and this reader now accepts too, so it proves nothing here.
        self.opencode_config('{"mcp": {"github": {}\n')
        result = self.run_tool("capture")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("does not parse", result.stderr)
        self.assertEqual(self.inventory(), before)

    def test_inline_comments_and_trailing_commas_parse(self):
        """REGRESSION: the reader removed whole-line `//` and nothing else.

        opencode documents JSONC, which is an inline `// note` after a value,
        a `/* */` block, and a comma before a closing bracket. Since an
        unparseable config is now fatal for the whole capture, each of those
        turned one tolerated file into a nightly that wrote nothing at all.
        """
        self.opencode_config(
            '{\n'
            '  "mcp": {"github": {}, /* the one this machine reaches */},\n'
            '  "plugin": ["./plugins/claude-mem.js",], // installed here\n'
            '  "note": "a // b /* c */ stays inside the string"\n'
            '}\n'
        )
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("opencode|mcp|github", self.inventory())
        self.assertIn("opencode|plugin|claude-mem", self.inventory())

    def test_a_missing_config_is_not_a_failure(self):
        """PIN: an app this machine does not run contributes nothing."""
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("opencode|", self.inventory())

    def test_the_nightly_fails_when_the_capture_could_not_run(self):
        """REGRESSION: `cmd_capture ... || true` discarded the refusal.

        The night then emailed and exited 0,
        leaving the inventory silently a day stale with nothing saying so.
        """
        # Truncated, not merely JSONC: the reader accepts a trailing comma.
        self.opencode_config('{"mcp": {"github": {}\n')
        result = self.run_tool("nightly")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("capture failed", result.stderr)


class RowKeyTest(Fixture):
    """Two plugins that share a display name stay two rows (sd:1273).

    The row was `app|kind|name` in a set, so the display name was the key.
    Two specs that derive one name lost a row, and the lost row then hid the
    next removal: the capture reported nothing when one of them went away.
    """

    def plugin_rows(self):
        return [line for line in self.inventory().splitlines()
                if line.startswith("opencode|plugin|")]

    @staticmethod
    def key(spec):
        return hashlib.sha256(spec.encode()).hexdigest()[:12]

    def test_two_plugins_with_one_name_both_survive(self):
        """REGRESSION: alpha/dist/main.js and beta/dist/main.js were one row."""
        self.opencode_config(
            '{"plugin": ["alpha/dist/main.js", "beta/dist/main.js"]}\n')
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.plugin_rows(), sorted([
            "opencode|plugin|main|" + self.key("alpha/dist/main.js"),
            "opencode|plugin|main|" + self.key("beta/dist/main.js"),
        ]))

    def test_specs_that_escape_alike_stay_two_rows(self):
        """REGRESSION: escaping | as %7C made these two specs one key."""
        self.opencode_config(
            '{"plugin": ["/opt/alpha|beta/dist/main.js",'
            ' "/opt/alpha%7Cbeta/dist/main.js"]}\n')
        self.assertEqual(self.run_tool("capture").returncode, 0)

        self.assertEqual(len(self.plugin_rows()), 2, self.inventory())

    def test_the_spec_itself_is_never_written(self):
        """REGRESSION: the key was the spec, credentials and all.

        The manifest is kept and mailed by the nightly, so a
        spec carrying a URL password or a download token must not reach it.
        Only a digest of the spec is recorded.
        """
        spec = "https://user:secret@host/x.tgz"
        token = "https://host/dl?token=tok123"
        self.opencode_config('{"plugin": ["%s", "%s"]}\n' % (spec, token))
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for text in (self.inventory(), result.stdout, result.stderr):
            self.assertNotIn("secret", text)
            self.assertNotIn("tok123", text)
            self.assertNotIn("://", text)
        self.assertEqual(self.plugin_rows(), sorted([
            "opencode|plugin|x|" + self.key(spec),
            "opencode|plugin|dl|" + self.key(token),
        ]))

    def test_removing_one_of_two_is_reported(self):
        """REGRESSION: the removal hid behind the surviving namesake."""
        self.opencode_config(
            '{"plugin": ["./plugins/index.js", "./plugins/plugin.js"]}\n')
        self.assertEqual(self.run_tool("capture").returncode, 0)

        self.opencode_config('{"plugin": ["./plugins/index.js"]}\n')
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gone:  opencode|plugin|plugins|"
                      + self.key("./plugins/plugin.js"), result.stdout)
        self.assertNotIn("  none", result.stdout)

    def test_home_is_replaced_only_as_a_leading_prefix(self):
        """REGRESSION: every occurrence of home became ~, not the prefix.

        /tmp<home>/x.js and /tmp~/x.js are two plugins, and they hashed alike.
        """
        inner = "/tmp%s/x.js" % self.home
        self.opencode_config('{"plugin": ["%s", "/tmp~/x.js"]}\n' % inner)
        self.assertEqual(self.run_tool("capture").returncode, 0)

        self.assertEqual(self.plugin_rows(), sorted([
            "opencode|plugin|x|" + self.key(inner),
            "opencode|plugin|x|" + self.key("/tmp~/x.js")]))

    def test_a_delimiter_in_a_label_keeps_four_fields(self):
        """REGRESSION: /opt/alpha|beta/index.js wrote a five-field row."""
        self.opencode_config('{"plugin": ["/opt/alpha|beta/index.js"]}\n')
        self.assertEqual(self.run_tool("capture").returncode, 0)

        self.assertEqual(self.plugin_rows(), [
            "opencode|plugin|alpha%7Cbeta|" + self.key("/opt/alpha|beta/index.js")])

    def test_readers_decode_an_encoded_label(self):
        """REGRESSION: the manifest encodes % and |, and no reader decoded.

        A skill folder named `foo%bar` was recorded as `foo%25bar`, which
        `setup` then looked for on disk and `compare` printed.
        """
        (self.home / ".claude" / "skills" / "foo%bar").mkdir(parents=True)
        (self.home / ".codex" / "skills").mkdir(parents=True)
        (self.folder / "profiles" / "personal.inv").write_text(
            FORMAT + "claude-code|skill|foo%25bar\ncodex|skill|foo%25bar\n")
        result = self.run_tool("setup", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("copied skill 'foo%bar' from claude-code to codex", result.stdout)
        self.assertTrue((self.home / ".codex" / "skills" / "foo%bar").is_dir())
        compare = self.run_tool("compare").stdout
        self.assertIn("foo%bar", compare)
        self.assertNotIn("%25", compare)

    def test_a_schemeless_spec_keeps_its_query_out(self):
        """REGRESSION: only a spec with :// had its query and userinfo dropped.

        ./x.js?token=.. and //user:pw@host/y.js wrote the token into the label.
        The tokens end in a dot-suffix, so dropping an extension cannot hide one.
        """
        rel = "./plugins/x.js?token=tok123.v1#frag.v2"
        net = "//user:secret@host/y.js?token=tok456.v1"
        self.opencode_config('{"plugin": ["%s", "%s"]}\n' % (rel, net))
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for text in (self.inventory(), result.stdout):
            for secret in ("tok123", "tok456", "secret", "frag"):
                self.assertNotIn(secret, text)
        self.assertEqual(self.plugin_rows(), sorted([
            "opencode|plugin|x|" + self.key(rel),
            "opencode|plugin|y|" + self.key(net)]))

    def test_an_old_raw_key_is_not_reported(self):
        """REGRESSION: a profile from the spec-keyed revision leaked on capture.

        That revision wrote the spec itself as the fourth field. The first
        capture after it reported the row as gone, spec and all, and the
        nightly mails that report.
        """
        (self.folder / "profiles" / "personal.inv").write_text(
            "opencode|plugin|x|https://user:secret@host/x.tgz\n")
        self.opencode_config('{"plugin": ["https://host/x.tgz"]}\n')
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("secret", result.stdout)
        self.assertIn("gone:  opencode|plugin|x|(old key withheld)", result.stdout)
        self.assertIn("new:   opencode|plugin|x|" + self.key("https://host/x.tgz"),
                      result.stdout)

    def test_profile_compare_and_setup_extras_decode(self):
        """REGRESSION: compare <p1> <p2> and setup's extra rows printed %25.

        compare also withholds an old raw key, as the capture report does.
        """
        profiles = self.folder / "profiles"
        (profiles / "personal.inv").write_text(
            FORMAT + "claude-code|skill|foo%25bar\n"
            "opencode|plugin|x|https://user:secret@host/x.tgz\n")
        (profiles / "work.inv").write_text(FORMAT + "codex|skill|a%7Cb\n")
        compare = self.run_tool("compare", "personal", "work")

        self.assertEqual(compare.returncode, 0, compare.stdout + compare.stderr)
        self.assertIn("  claude-code|skill|foo%bar\n", compare.stdout)
        self.assertIn("  codex|skill|a|b\n", compare.stdout)
        self.assertIn("  opencode|plugin|x|(old key withheld)\n", compare.stdout)
        self.assertNotIn("secret", compare.stdout)

        (self.home / ".claude" / "skills" / "baz%qux").mkdir(parents=True)
        (profiles / "personal.inv").write_text("")
        setup = self.run_tool("setup")
        self.assertIn("  claude-code|skill|baz%qux\n", setup.stdout,
                      setup.stdout + setup.stderr)

    def test_setup_reads_an_old_manifest_label_literally(self):
        """REGRESSION (sd:1826): setup decoded labels that were never encoded.

        A manifest from before the encoding holds a skill `100%25` as
        written. setup read it as `100%` and so did not copy the folder.
        """
        (self.home / ".claude" / "skills" / "100%25").mkdir(parents=True)
        (self.home / ".codex" / "skills").mkdir(parents=True)
        for header in (OLD_FORMAT, ""):
            with self.subTest(header=header):
                shutil.rmtree(self.home / ".codex" / "skills" / "100%25",
                              ignore_errors=True)
                (self.folder / "profiles" / "personal.inv").write_text(
                    header + "claude-code|skill|100%25\ncodex|skill|100%25\n")
                result = self.run_tool("setup", "--apply")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("copied skill '100%25' from claude-code to codex",
                              result.stdout)
                self.assertTrue((self.home / ".codex" / "skills" / "100%25").is_dir())

    def test_compare_prints_an_old_manifest_label_as_written(self):
        """REGRESSION (sd:1826): compare <p1> <p2> decoded an old literal label."""
        profiles = self.folder / "profiles"
        (profiles / "personal.inv").write_text(OLD_FORMAT + "codex|skill|100%25\n")
        (profiles / "work.inv").write_text(FORMAT + "codex|skill|100%2525\n")
        compare = self.run_tool("compare", "personal", "work")

        self.assertEqual(compare.returncode, 0, compare.stdout + compare.stderr)
        self.assertIn("no differences between personal and work", compare.stdout)

    def test_capture_migrates_an_old_manifest_without_a_change_report(self):
        """REGRESSION (sd:1826): the capture report decoded the old label.

        It reported skill `100%` as gone and `100%25` as new, for a skill
        that never changed. The file is still rewritten, encoded.
        """
        (self.home / ".claude" / "skills" / "100%25").mkdir(parents=True)
        inv = self.folder / "profiles" / "personal.inv"
        inv.write_text(OLD_FORMAT + "claude-code|skill|100%25\n")
        result = self.run_tool("capture")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  none", result.stdout)
        self.assertIn(FORMAT, inv.read_text())
        self.assertIn("claude-code|skill|100%2525\n", inv.read_text())

    def test_the_key_is_the_same_under_any_home(self):
        """PIN: home becomes ~ before hashing, so two machines agree."""
        self.opencode_config(
            '{"plugin": ["file://%s/src/x/dist/index.js"]}\n' % self.home)
        self.assertEqual(self.run_tool("capture").returncode, 0)

        self.assertEqual(self.plugin_rows(), [
            "opencode|plugin|dist|" + self.key("file://~/src/x/dist/index.js")])

    def test_a_header_only_difference_is_not_reported(self):
        """REGRESSION: only the dated header line was kept out of the report.

        The format line changed with the key, so the first capture after it
        reported the comment as a gone and a new inventory row.
        """
        self.opencode_config('{"plugin": ["./plugins/claude-mem.js"]}\n')
        self.assertEqual(self.run_tool("capture").returncode, 0)
        inv = self.folder / "profiles" / "personal.inv"
        inv.write_text(re.sub(r"(?m)^# format: .*$", "# format: an older one",
                              inv.read_text()))

        result = self.run_tool("capture")

        self.assertIn("  none", result.stdout, result.stdout + result.stderr)
        self.assertNotIn("# format", result.stdout)

    def test_setup_and_the_matrix_read_keyed_rows(self):
        """PIN: the readers split on the first three fields, not the line."""
        self.opencode_config(
            '{"plugin": ["alpha/dist/main.js", "beta/dist/main.js"]}\n')
        self.assertEqual(self.run_tool("capture").returncode, 0)

        setup = self.run_tool("setup")
        self.assertIn("machine matches profile 'personal'", setup.stdout,
                      setup.stdout + setup.stderr)

        matrix = self.run_tool("compare").stdout
        row = [line for line in matrix.splitlines()
               if line.strip().startswith("main ")]
        self.assertEqual(len(row), 1, matrix)
        self.assertEqual(row[0].split()[1:], ["-", "-", "-", "x", "-"])



class WeeklyOnlyApps(Fixture):
    def test_update_leaves_codex_and_claude_code_to_the_weekly_upgrade(self):
        """sd:3033: codex and Claude Code update in the weekly upgrade only."""
        self._double(self.bin / "brew", BREW_WEEKLY_ONLY_STUB)
        brew_log = self.tmp / "brew.log"
        result = self.run_tool("update", extra_env={"BREW_LOG": str(brew_log)})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all AI apps current", result.stdout)
        self.assertFalse(brew_log.exists(), brew_log.read_text() if brew_log.exists() else "")


if __name__ == "__main__":
    unittest.main()
